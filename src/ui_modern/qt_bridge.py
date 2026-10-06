"""Thread boundary for core results, events and blocking prompts."""
from concurrent.futures import Future
from dataclasses import dataclass, field
import threading
import logging
from PySide6.QtCore import QObject, Signal, Slot, Qt, QThread, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog
from qfluentwidgets import MessageBox, MessageBoxBase, LineEdit, ComboBox, CheckBox, BodyLabel, ProgressBar
from app_core import events


class _Dispatcher(QObject):
    requested = Signal(object)
    def __init__(self):
        super().__init__()
        self.requested.connect(self.run, Qt.QueuedConnection)
    @Slot(object)
    def run(self, fn):
        fn()

_dispatcher = None

def gui_dispatcher():
    global _dispatcher
    if _dispatcher is None:
        assert QThread.currentThread() == QApplication.instance().thread()
        _dispatcher = _Dispatcher()
    return _dispatcher


def call_core(fn, *args, on_result=None, on_error=None, **kwargs):
    """Follow all Future layers without waiting; completion always queues to Qt."""
    dispatch = gui_dispatcher()
    def fail(error):
        if on_error:
            dispatch.requested.emit(lambda: on_error(error))
    def follow(value):
        if isinstance(value, Future):
            def done(future):
                try:
                    result = future.result()  # callback runs only after completion
                except BaseException as error:
                    fail(error)
                else:
                    follow(result)
            value.add_done_callback(done)
        elif on_result:
            dispatch.requested.emit(lambda: on_result(value))
    try:
        follow(fn(*args, **kwargs))
    except Exception as error:
        fail(error)


_TOPICS = tuple(value for name, value in vars(events).items() if name.isupper() and isinstance(value, str))
class BusBridge(QObject):
    event = Signal(str, object)
    incoming = Signal(str, object)
    for _topic in _TOPICS:
        locals()[_topic] = Signal(object)
    def __init__(self, bus, parent=None):
        super().__init__(parent)
        self._latest = {}
        self._flush_pending = False
        self.incoming.connect(self._receive, Qt.QueuedConnection)
        self._unsubscribers = [bus.subscribe(topic, lambda payload, t=topic: self.incoming.emit(t, payload)) for topic in _TOPICS]
    @Slot(str, object)
    def _receive(self, topic, payload):
        if topic in (events.CONTROLLERS_CHANGED, events.CALIBRATION_SESSION):
            self._latest[topic] = payload
            if not self._flush_pending:
                self._flush_pending = True
                QTimer.singleShot(0, self._flush)
        else:
            self._emit(topic, payload)
    def _flush(self):
        pending, self._latest = self._latest, {}
        self._flush_pending = False
        for topic, payload in pending.items():
            self._emit(topic, payload)
    def _emit(self, topic, payload):
        getattr(self, topic).emit(payload)
        self.event.emit(topic, payload)
    def close(self):
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()


@dataclass(eq=False)
class _Request:
    name: str
    args: tuple
    kwargs: dict
    default: object = None
    done: threading.Event = field(default_factory=threading.Event)
    value: object = None
    dialog: object = None

class _ProgressDialog(MessageBoxBase):
    def __init__(self, title, message, cancellable, parent):
        super().__init__(parent)
        self.can_close = False
        self.cancel_event = threading.Event()
        self.viewLayout.addWidget(BodyLabel(title, self))
        self.label = BodyLabel(message, self)
        self.viewLayout.addWidget(self.label)
        self.bar = ProgressBar(self)
        self.viewLayout.addWidget(self.bar)
        self.hideYesButton()
        self.cancelButton.setVisible(cancellable)
        self.cancelButton.clicked.connect(self.cancel_event.set)
    def reject(self):
        if self.can_close:
            super().reject()
    def closeEvent(self, event):
        if self.can_close:
            super().closeEvent(event)
        else:
            event.ignore()

class _ClosedProgress:
    """Shutdown default must still satisfy ProgressHandle for unwinding workers."""
    cancelled = True
    def update(self, message=None, fraction=None):
        pass
    def close(self):
        pass

class _ProgressHandle:
    def __init__(self, port, dialog):
        self.port, self.dialog = port, dialog
    @property
    def cancelled(self):
        return self.dialog.cancel_event.is_set()
    def update(self, message=None, fraction=None):
        def apply():
            if message is not None:
                self.dialog.label.setText(message)
            if fraction is not None:
                self.dialog.bar.setValue(round(fraction * 100))
        self.port.dispatch.requested.emit(apply)
    def close(self):
        def apply():
            self.dialog.can_close = True
            self.dialog.accept()
            self.port._progress.discard(self.dialog)
        self.port.dispatch.requested.emit(apply)

class QtUiPort(QObject):
    requested = Signal(object)
    def __init__(self, parent=None, *, test_hook=None):
        super().__init__(parent)
        self.window = None
        self.profiles = None
        self.test_hook = test_hook
        self.dispatch = gui_dispatcher()
        self._pending = set()
        self._progress = set()
        self._lock = threading.Lock()
        self._closing = False
        self._hwnd = None
        self.requested.connect(self._show, Qt.QueuedConnection)
    def bind(self, window, profiles):
        self.window, self.profiles = window, profiles
        self._hwnd = int(window.winId())
    def native_window_handle(self):
        return self._hwnd
    def _request(self, name, args, kwargs, default=None):
        assert QThread.currentThread() != self.thread(), "UiPort prompts cannot run on the GUI thread"
        request = _Request(name, args, kwargs, default)
        with self._lock:
            if self._closing:
                return default
            self._pending.add(request)
        self.requested.emit(request)
        request.done.wait()
        return request.value
    def _finish(self, request, value):
        with self._lock:
            if request.done.is_set():
                return
            request.value = value
            self._pending.discard(request)
            request.done.set()
    @Slot(object)
    def _show(self, r):
        try:
            self._display(r)
        except Exception:
            logging.getLogger(__name__).exception("Unable to display UI prompt")
            self._finish(r, r.default)

    def _display(self, r):
        if r.done.is_set():
            return
        if self.test_hook:
            self._finish(r, self.test_hook(r))
            return
        if self.window is not None:
            if not self.window.isVisible():
                self.window.showNormal()
            self.window.raise_()
        title, message = r.args[:2]
        if r.name in ("ask_open_file", "ask_save_file"):
            # Enter/exit may wait for core: keep the suspension on the blocked caller.
            dialog = QFileDialog(self.window, title)
            dialog.setNameFilters([f"{label} ({pattern})" for label, pattern in message])
            if r.name == "ask_save_file":
                dialog.setAcceptMode(QFileDialog.AcceptSave)
                dialog.selectFile(r.kwargs.get("default_name", ""))
            else:
                dialog.setFileMode(QFileDialog.ExistingFile)
            r.dialog = dialog
            dialog.finished.connect(lambda accepted: self._finish(r, dialog.selectedFiles()[0] if accepted and dialog.selectedFiles() else None))
            dialog.open()
            return
        if r.name == "progress":
            dialog = _ProgressDialog(title, message, r.kwargs.get("cancellable", False), self.window)
            self._progress.add(dialog)
            dialog.show()
            self._finish(r, _ProgressHandle(self, dialog))
            return
        if r.name in ("ask_string", "ask_choice", "ask_yes_no_opt_out"):
            dialog = MessageBoxBase(self.window)
            dialog.viewLayout.addWidget(BodyLabel(title, dialog))
            dialog.viewLayout.addWidget(BodyLabel(message, dialog))
            if r.name == "ask_string":
                field = LineEdit(dialog); field.setText(r.kwargs.get("initial", ""))
                result = lambda accepted: field.text() if accepted else None
            elif r.name == "ask_choice":
                field = ComboBox(dialog); field.addItems(list(r.args[2]))
                if r.kwargs.get("default") in r.args[2]:
                    field.setCurrentText(r.kwargs["default"])
                result = lambda accepted: field.currentText() if accepted else None
            else:
                field = CheckBox(r.kwargs["opt_out_label"], dialog)
                result = lambda accepted: (bool(accepted), field.isChecked())
            dialog.viewLayout.addWidget(field)
        else:
            dialog = MessageBox(title, message, self.window)
            result = lambda accepted: bool(accepted) if r.name == "ask_yes_no" else None
            if r.name == "show_message":
                dialog.cancelButton.hide()
        if r.kwargs.get("danger"):
            dialog.yesButton.setStyleSheet("background-color: #c42b1c; color: white;")
        dialog.yesButton.setText(r.kwargs.get("yes", "Yes" if r.name == "ask_yes_no" else "OK"))
        dialog.cancelButton.setText(r.kwargs.get("no", "Cancel"))
        r.dialog = dialog
        dialog.finished.connect(lambda accepted: self._finish(r, result(accepted)))
        dialog.show()
    def cancel_all(self):
        with self._lock:
            self._closing = True
            pending = list(self._pending)
        for r in pending:
            self._finish(r, r.default)
            if r.dialog:
                r.dialog.reject()
        for dialog in list(self._progress):
            dialog.can_close = True
            dialog.cancel_event.set()
            dialog.reject()
        self._progress.clear()
    def ask_yes_no(self, title, message, **kwargs):
        return self._request("ask_yes_no", (title, message), kwargs, False)
    def ask_yes_no_opt_out(self, title, message, **kwargs):
        return self._request("ask_yes_no_opt_out", (title, message), kwargs, (False, False))
    def ask_choice(self, title, message, choices, *, default=None):
        return self._request("ask_choice", (title, message, choices), {"default": default})
    def show_message(self, title, message, *, kind="info"):
        return self._request("show_message", (title, message), {"kind": kind})
    def ask_string(self, title, prompt, *, initial=""):
        return self._request("ask_string", (title, prompt), {"initial": initial})
    def _file(self, name, title, filters, **kwargs):
        from contextlib import nullcontext
        assert QThread.currentThread() != self.thread()
        with self.profiles.suspend_focus_polling() if self.profiles else nullcontext():
            return self._request(name, (title, filters), kwargs)
    def ask_open_file(self, title, filters):
        return self._file("ask_open_file", title, filters)
    def ask_save_file(self, title, filters, *, default_name=""):
        return self._file("ask_save_file", title, filters, default_name=default_name)
    def progress(self, title, message="", *, cancellable=False):
        return self._request("progress", (title, message), {"cancellable": cancellable}, _ClosedProgress())
