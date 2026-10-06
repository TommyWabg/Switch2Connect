import logging

import pytest

from app_core.ui_port import NullUiPort, ProgressHandle, ScriptedUiPort, UiPort


def test_scripted_questions_record_defaults_and_consume_answers():
    ui = ScriptedUiPort([True, "B", "typed", "open.yaml", None])
    assert isinstance(ui, UiPort)
    assert ui.ask_yes_no("Delete", "Sure?", yes="Delete", danger=True) is True
    ui.show_message("Result", "Done", kind="warning")
    handle = ui.progress("Task", "Working", cancellable=True)
    assert ui.ask_choice("Pick", "Which?", ["A", "B"], default="A") == "B"
    assert ui.ask_string("Name", "Enter", initial="old") == "typed"
    assert ui.ask_open_file("Open", [("YAML", "*.yaml")]) == "open.yaml"
    assert ui.ask_save_file("Save", [], default_name="x.yaml") is None
    assert not ui.answers
    assert [call.name for call in ui.calls] == [
        "ask_yes_no", "show_message", "progress", "ask_choice", "ask_string",
        "ask_open_file", "ask_save_file",
    ]
    assert ui.calls[0].args == ("Delete", "Sure?")
    assert ui.calls[0].kwargs == {"yes": "Delete", "no": "No", "danger": True}
    assert ui.calls[3].args == ("Pick", "Which?", ("A", "B"))
    assert ui.calls[-1].kwargs == {"default_name": "x.yaml"}
    assert isinstance(handle, ProgressHandle)


def test_exhausted_script_fails_and_records_question():
    ui = ScriptedUiPort()
    with pytest.raises(AssertionError, match="ask_yes_no"):
        ui.ask_yes_no("Title", "Message")
    assert ui.calls[-1].name == "ask_yes_no"
    ui.answers.append(False)
    assert ui.ask_yes_no("Title", "Message") is False


def test_progress_update_cancel_and_close():
    ui = ScriptedUiPort([True])
    handle = ui.progress("Install", "Starting", cancellable=True)
    assert not handle.cancelled
    handle.update("Downloading", 0.25)
    handle.update(fraction=0.5)
    assert handle.message == "Downloading"
    assert handle.fraction == 0.5
    handle.cancel()
    assert handle.cancelled
    handle.close()
    handle.close()
    assert handle.closed
    assert list(ui.answers) == [True]
    fixed = ui.progress("Task")
    fixed.cancel()
    assert not fixed.cancelled
    assert "progress.update" in [call.name for call in ui.calls]


def test_null_declines_and_logs_every_operation(caplog):
    caplog.set_level(logging.INFO)
    ui = NullUiPort()
    assert ui.ask_yes_no("t", "m") is False
    assert ui.ask_choice("t", "m", ["yes"], default="yes") is None
    assert ui.ask_string("t", "p", initial="s") is None
    assert ui.ask_open_file("t", []) is None
    assert ui.ask_save_file("t", []) is None
    ui.show_message("t", "m")
    handle = ui.progress("t")
    handle.update("m", 0.5)
    assert not handle.cancelled
    handle.close()
    assert len(caplog.records) == 10


def test_optout_tuple_and_native_handle_defaults():
    null = NullUiPort()
    assert null.ask_yes_no_opt_out("Title", "Message", opt_out_label="Hide") == (False, False)
    assert null.native_window_handle() is None
    ui = ScriptedUiPort([(False, True)])
    assert ui.native_window_handle() is None
    assert list(ui.answers) == [(False, True)]
    assert ui.ask_yes_no_opt_out("Title", "Message", opt_out_label="Hide", yes="Proceed", no="Cancel") == (False, True)
    assert not ui.answers
    assert ui.calls[-1].kwargs == {"opt_out_label": "Hide", "yes": "Proceed", "no": "Cancel"}
