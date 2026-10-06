from concurrent.futures import Future
import threading
import pytest
from PySide6.QtCore import QThread
from app_core import events
from app_core.bus import EventBus
from ui_modern.qt_bridge import QtUiPort, BusBridge, call_core


def test_roundtrip_and_assert_gui(app, pump):
    threads = []
    port = QtUiPort(test_hook=lambda r: (threads.append(QThread.currentThread()), True)[1])
    answers = []
    worker = threading.Thread(target=lambda: answers.append(port.ask_yes_no("Test", "Continue?")))
    worker.start(); pump(lambda: not worker.is_alive(), 2); worker.join(timeout=1)
    assert answers == [True] and threads == [app.thread()]
    with pytest.raises(AssertionError):
        port.ask_yes_no("Test", "GUI")


def test_cancel_all_pending_and_late(app, pump):
    port = QtUiPort(); port.requested.disconnect()
    answers = []
    worker = threading.Thread(target=lambda: answers.append(port.ask_choice("Test", "Choose", ["a"], default="a")))
    worker.start(); pump(lambda: bool(port._pending), 1)
    port.cancel_all(); worker.join(timeout=1)
    assert answers == [None]
    late = threading.Thread(target=lambda: answers.append(port.ask_yes_no("Test", "Late")))
    late.start(); late.join(timeout=1)
    assert answers == [None, False]


def test_nested_future_and_error(app, pump):
    outer, inner = Future(), Future(); results = []; errors = []
    call_core(lambda: outer, on_result=lambda value: results.append((value, QThread.currentThread())), on_error=errors.append)
    def complete():
        outer.set_result(inner); inner.set_result(42)
    worker = threading.Thread(target=complete); worker.start(); worker.join(); pump()
    assert results == [(42, app.thread())]
    failed = Future(); call_core(lambda: failed, on_error=errors.append)
    failed.set_exception(ValueError("failed")); pump()
    assert isinstance(errors[0], ValueError)


def test_bus_topics_coalescing_and_gui(app, pump):
    bus = EventBus(); bridge = BusBridge(bus); emitted = []
    bridge.event.connect(lambda topic, payload: emitted.append((topic, payload, QThread.currentThread())))
    def publish():
        for i in range(50):
            bus.publish(events.CONTROLLERS_CHANGED, i)
        bus.publish(events.STATUS_CHANGED, "ready")
    worker = threading.Thread(target=publish); worker.start(); worker.join(); pump()
    controllers = [item for item in emitted if item[0] == events.CONTROLLERS_CHANGED]
    assert len(controllers)==1 and controllers[0][1]==49
    assert all(item[2]==app.thread() for item in emitted)
    for name, topic in vars(events).items():
        if name.isupper() and isinstance(topic, str):
            assert hasattr(bridge, topic)
    bridge.close(); count=len(emitted); bus.publish(events.STATUS_CHANGED); pump(); assert len(emitted)==count


def test_actual_dialogs_and_progress(window, pump):
    from app_core.ui_port import UiPort
    from ui_modern.qt_bridge import _ProgressHandle
    from qfluentwidgets import CheckBox
    w, core = window
    port = QtUiPort(); port.bind(w, core.profiles)
    assert isinstance(port, UiPort)
    def ask(fn, answer):
        result=[]
        worker=threading.Thread(target=lambda: result.append(fn()))
        worker.start()
        pump(lambda: any(r.dialog is not None for r in port._pending), 2)
        request=next(iter(port._pending))
        answer(request.dialog)
        pump(lambda: not worker.is_alive(), 2); worker.join(timeout=1)
        assert not worker.is_alive()
        return result[0]
    w.hide()
    assert ask(lambda: port.ask_yes_no("Test", "Continue?", yes="Proceed", danger=True), lambda d: d.accept()) is True
    assert w.isVisible()
    assert ask(lambda: port.ask_string("Name", "Enter a name", initial="Sample"), lambda d: d.accept()) == "Sample"
    assert ask(lambda: port.ask_choice("Choice", "Choose", ("A", "B"), default="B"), lambda d: d.accept()) == "B"
    assert ask(lambda: port.ask_yes_no_opt_out("Warning", "Continue?", opt_out_label="Do not show again"), lambda d: (d.findChild(CheckBox).setChecked(True), d.reject())) == (False, True)
    assert ask(lambda: port.show_message("Info", "Message"), lambda d: d.accept()) is None
    result=[]
    worker=threading.Thread(target=lambda: result.append(port.progress("Work", "Running", cancellable=True)))
    worker.start(); pump(lambda: not worker.is_alive(), 2); worker.join(timeout=1)
    handle=result[0]; assert isinstance(handle, _ProgressHandle)
    handle.dialog.reject(); assert handle.dialog.isVisible()
    handle.dialog.cancelButton.click(); assert handle.cancelled and handle.dialog.isVisible()
    handle.update("Halfway", .5); pump()
    assert handle.dialog.label.text()=="Halfway" and handle.dialog.bar.value()==50
    handle.close(); pump(lambda: not handle.dialog.isVisible(), 1); assert not handle.dialog.isVisible()
    port.cancel_all()


def test_file_polling_suspension_and_progress_shutdown_default(window, pump):
    w, core = window
    port=QtUiPort(test_hook=lambda r: "selected.json"); port.bind(w, core.profiles)
    results=[]
    worker=threading.Thread(target=lambda: results.append(port.ask_open_file("Import", [("JSON", "*.json")])))
    worker.start(); pump(lambda: not worker.is_alive(), 2); worker.join(timeout=1)
    assert results==["selected.json"]
    assert core.calls[-2:]==[("profiles.suspend_enter", (), {}), ("profiles.suspend_exit", (), {})]
    port.cancel_all()
    worker=threading.Thread(target=lambda: results.append(port.progress("Late")))
    worker.start(); worker.join(timeout=1)
    assert results[-1].cancelled
    results[-1].update("ignored", .5); results[-1].close()
