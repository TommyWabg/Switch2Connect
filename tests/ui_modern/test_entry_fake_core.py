from dataclasses import replace
from PySide6.QtCore import QTimer
from ui_modern.app import build_session, main
from conftest import FakeCore


def test_construction_lifecycle(app, pump):
    exits=[]; session=build_session(app, FakeCore, exit_process=exits.append)
    session.window.setMicaEffectEnabled(False)
    session.start(); pump()
    assert session.window.isVisible()
    assert session.ui_port.native_window_handle()
    assert session.core.calls.count(("start", (), {}))==1
    session.quit(); session.quit(); pump()
    assert exits==[0]
    assert session.core.calls.count(("shutdown", (), {}))==1
    assert not session.window.isVisible()
    session.window.deleteLater(); pump()


def test_entry_main_minimized(app, pump):
    exits=[]
    def factory(port):
        core=FakeCore(port)
        core.settings_state=replace(core.settings_state, app=replace(core.settings_state.app, start_minimized=True))
        def check():
            assert not port.window.isVisible()
            port.window.quit_requested.emit(False)
        QTimer.singleShot(80, check)
        return core
    assert main(["fake-modern"], core_factory=factory, exit_process=exits.append)==0
    assert exits==[0]


def test_restart_runs_after_core_cleanup(app, pump, monkeypatch):
    import ui_modern.app as entry
    order=[]
    session=build_session(app, FakeCore, exit_process=lambda code: order.append(("exit", code)))
    monkeypatch.setattr(entry, "schedule_relaunch", lambda: order.append(("relaunch", session.core.calls[-1][0])))
    session.quit(True); pump()
    assert order==[("relaunch", "shutdown"), ("exit", 0)]
    session.window.deleteLater(); pump()
