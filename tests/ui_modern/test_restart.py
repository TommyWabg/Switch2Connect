import sys
from pathlib import Path
import pytest
from ui_modern.restart import relaunch_args, resolve_ui_mode, schedule_relaunch

@pytest.mark.parametrize("argv,config,expected", [([], "modern", "modern"), (["--ui=classic"], "modern", "classic"), (["--ui=modern"], "classic", "modern"), (["--ui=wrong"], "unknown", "classic"), (["--ui=modern", "--ui=classic"], "modern", "classic")])
def test_mode(argv, config, expected):
    assert resolve_ui_mode(argv, config)==expected

@pytest.mark.parametrize("frozen", [True, False])
def test_args(monkeypatch, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["launcher", "--ui=modern", "--debug", "--ui=classic"])
    args = relaunch_args()
    assert args[0]==sys.executable and args[-1]=="--debug"
    assert not any(arg.startswith("--ui=") for arg in args)
    assert len(args)==(2 if frozen else 3)
    if not frozen:
        assert Path(args[1]).name=="gui.py"


def test_detached_spawn(monkeypatch):
    import ui_modern.restart as restart
    calls=[]; monkeypatch.setattr(restart.subprocess, "Popen", lambda *a, **k: calls.append((a,k)))
    schedule_relaunch()
    assert calls[0][1]["close_fds"]
    assert calls[0][1].get("creationflags", calls[0][1].get("start_new_session"))


def test_classic_dispatch_uses_helper():
    source=(Path(__file__).resolve().parents[2]/"src/gui.py").read_text(encoding="utf-8")
    tail=source[source.rindex('if __name__ == "__main__":'):]
    assert tail.index("disable_power_throttling()") < tail.index("resolve_ui_mode(sys.argv, CONFIG.ui_mode)") < tail.index("win = ControllerWindow()")
    assert 'sys.exit(_modern_main(sys.argv))' in tail
    assert 'except ImportError:' in tail
