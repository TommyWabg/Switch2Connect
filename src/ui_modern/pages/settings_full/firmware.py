"""Pure view of Esp32Service snapshots and FirmwareSession events."""
from ...widgets.changes import apply_change, show_error
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget, QHBoxLayout
from qfluentwidgets import BodyLabel, PushButton, ComboBox, ProgressBar, qconfig, isDarkTheme
from app_core import events
from ...qt_bridge import BusBridge, call_core
from ...widgets.common import SettingsGroup, Segments
from ..rumble.support import sync


class FirmwareManagerDialog(QDialog):
    def __init__(self, core, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setWindowTitle('ESP32-S3 firmware manager'); self.resize(640, 460)
        self.setObjectName('firmwareManager')
        qconfig.themeChangedFinished.connect(self.refresh_surface); self.refresh_surface()
        self.can_close = True; self.awaiting_replug = False; self.busy = False
        self.session_done = False; self.verified_replug = False
        box = QVBoxLayout(self); box.setContentsMargins(24, 20, 24, 24); box.setSpacing(16)
        group = SettingsGroup('ESP32-S3 firmware', self); box.addWidget(group)
        self.status = BodyLabel('Checking…', group); self.status.setWordWrap(True); group.row('Status', self.status)
        self.mode = Segments(group); self.mode.sync(('auto', 'manual'), 'auto', {'auto': 'Automatic', 'manual': 'Manual'})
        group.row('Port mode', self.mode)
        holder = QWidget(group); line = QHBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0)
        self.ports = ComboBox(holder); self.ports.setMinimumWidth(280); self.ports.setAccessibleName('Firmware serial port')
        self.refresh = PushButton('Refresh', holder); line.addWidget(self.ports); line.addWidget(self.refresh)
        self.port_row = group.row('Serial port', holder); self.port_row.hide()
        self.mode.currentItemChanged.connect(self.select_mode)
        self.ports.currentIndexChanged.connect(self.select_port)
        self.refresh.clicked.connect(self.refresh_ports)
        self.guidance = BodyLabel('', self); self.guidance.setWordWrap(True); box.addWidget(self.guidance)
        self.progress = ProgressBar(self); self.progress.setRange(0, 100); box.addWidget(self.progress)
        self.phase = BodyLabel('Idle · 0%', self); self.phase.setWordWrap(True); box.addWidget(self.phase)
        self.error = BodyLabel('', self); self.error.setWordWrap(True); box.addWidget(self.error)
        actions = QHBoxLayout(); self.actions = {}
        for action in ('install', 'repair', 'delete'):
            button = PushButton(action.title(), self); self.actions[action] = button; actions.addWidget(button)
            button.clicked.connect(lambda checked=False, a=action: self.start(a))
        box.addLayout(actions)
        self.close_button = PushButton('Close', self); self.close_button.clicked.connect(self.reject); box.addWidget(self.close_button)
        self.bridge = BusBridge(core.bus, self); self.bridge.event.connect(self.on_bus_event)
        self.monitor = QTimer(self); self.monitor.setInterval(500)
        self.monitor.timeout.connect(lambda: call_core(core.esp32.refresh_manager_status_async))
        self.monitor.start()
        call_core(core.esp32.state, on_result=self.apply_state)
        call_core(core.esp32.port_mode, on_result=self.apply_mode)
        call_core(core.esp32.firmware_session, on_result=self.apply_session)
        self.refresh_ports()

    def refresh_surface(self):
        self.setStyleSheet('QDialog#firmwareManager { background-color: ' + ('#202020' if isDarkTheme() else '#f3f3f3') + '; }')

    def on_bus_event(self, topic, value):
        if topic == events.ESP32_STATE_CHANGED:
            self.apply_state(value)
            call_core(self.core.esp32.port_mode, on_result=self.apply_mode)
        elif topic == events.ESP32_FIRMWARE_SESSION: self.apply_session(value)
        elif topic == events.ESP32_PORTS_CHANGED: self.apply_ports(value)

    def apply_mode(self, mode):
        self.selected_port = mode.port
        self.mode.sync(('auto', 'manual'), mode.mode, {'auto': 'Automatic', 'manual': 'Manual'})
        self.port_row.setVisible(mode.mode == 'manual')
        for i in range(self.ports.count()):
            choice = self.ports.itemData(i)
            if choice.port.upper() == mode.port.upper(): sync(self.ports, 'setCurrentIndex', i)

    def apply_ports(self, ports):
        old = getattr(self, 'selected_port', '')
        self.ports.blockSignals(True); self.ports.clear()
        for choice in ports:
            self.ports.addItem(choice.label + ('' if choice.verified else ' · Unverified candidate'), userData=choice)
        for i, choice in enumerate(ports):
            if choice.port.upper() == old.upper(): self.ports.setCurrentIndex(i)
        self.ports.blockSignals(False)
        self.ports.setToolTip(self.ports.currentText())

    def select_mode(self, mode):
        self.port_row.setVisible(mode == 'manual')
        if mode == 'auto': apply_change(self, self.core.esp32.set_port_mode, 'Auto')
        else: self.select_port(self.ports.currentIndex())

    def select_port(self, index):
        if self.mode.currentRouteKey() == 'manual' and index >= 0:
            self.ports.setToolTip(self.ports.currentText())
            apply_change(self, self.core.esp32.set_port_mode, self.ports.itemData(index))

    def refresh_ports(self):
        call_core(self.core.esp32.list_ports, on_result=self.apply_ports, on_error=lambda exc: show_error(self, str(exc)))


    def start(self, action):
        self.error.setText('')
        # Erase confirmation and boot checks belong to the service / QtUiPort.
        choice = self.ports.currentData() if self.mode.currentRouteKey() == 'manual' else 'Auto'
        if choice is None:
            self.error.setText('Choose a serial port or use Automatic mode.')
            return
        apply_change(self, getattr(self.core.esp32, action), choice)

    def apply_state(self, state):
        self.busy = state.busy
        self.verified_replug = state.detected and state.firmware_installed and not state.unverified_candidate
        self.status.setText(' · '.join(filter(None, (state.status, state.firmware_text))))
        boot_required = state.transport in ('OTG', 'otg') and state.firmware_installed and not state.otg_boot
        for action, button in self.actions.items():
            button.setEnabled(not state.busy and not (boot_required and action in ('install', 'repair')))
        if not self.awaiting_replug:
            self.guidance.setText('Hold BOOT, tap RESET, release BOOT, then connect the CH343P / UART Type-C port and retry.' if boot_required or state.otg_boot else '')
        self.mode.setEnabled(not state.busy); self.ports.setEnabled(not state.busy); self.refresh.setEnabled(not state.busy)
        if self.awaiting_replug and self.session_done and self.verified_replug:
            self.accept()

    def apply_session(self, session):
        was_waiting = self.awaiting_replug
        self.can_close = session.can_close and session.phase != 'flashing'
        self.session_done = session.phase == 'done'
        self.awaiting_replug = session.phase == 'awaiting_replug' or (was_waiting and self.session_done)
        self.close_button.setEnabled(self.can_close)
        self.progress.setValue(round(session.percent))
        self.phase.setText(f'{session.phase.replace("_", " ").title()} · {session.percent:.0f}%\n{session.message}')
        if self.awaiting_replug: self.guidance.setText('Unplug the ESP32-S3 board, then reconnect it. This window closes after verified replug.')
        if session.phase == 'error': self.error.setText(session.message)
        if was_waiting and self.session_done and self.verified_replug: self.accept()

    def done(self, result):
        if not self.can_close: return
        self.monitor.stop(); self.bridge.close()
        call_core(self.core.esp32.close_firmware_session)
        super().done(result)

    def closeEvent(self, event):
        if not self.can_close: event.ignore()
        else:
            self.reject(); event.accept()
