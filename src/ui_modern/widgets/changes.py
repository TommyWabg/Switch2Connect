"""One asynchronous change/Undo path; successful changes render through bus events."""
from shiboken6 import isValid
import logging
from ..qt_bridge import call_core

logger = logging.getLogger(__name__)

def show_error(owner, message, row=None):
    if not isValid(owner) or getattr(owner, "_closed", False):
        return
    if row:
        row.captionLabel.setText(message)
        row.captionLabel.show()
    target = getattr(owner, "error", getattr(owner, "message", None))
    if hasattr(target, "setText"):
        target.setText(message)
    if callable(getattr(owner, "toast", None)):
        owner.toast(message, "error")


def apply_setting(owner, service, method, key, value, snapshot):
    apply_change(owner, getattr(service, method), value, old_args=(getattr(snapshot, key),))


def setting_writer(owner, fn, *prefix, read=None, suffix=(), **kwargs):
    def write(value):
        old_args = (*prefix, read(), *suffix) if read else None
        apply_change(owner, fn, *prefix, value, *suffix, old_args=old_args, **kwargs)
    return write


def apply_toggle(owner, fn, read):
    old = read()

    def restore():
        if read() != old:
            apply_change(owner, fn)

    apply_change(owner, fn, undo=restore)


def apply_change(owner, fn, *args, old_args=None, undo=None, label="Change applied",
                 row=None, after=None, restart=False, rollback=None, on_finished=None, **kwargs):
    if rollback is None:
        saved = [(state, getattr(owner, render))
                 for state, render in (("_settings_state", "apply_settings"),
                                       ("_driver_state", "apply_driver"),
                                       ("driver_state", "apply_driver"),
                                       ("state", "apply_profiles"), ("view", "apply_view"))
                 if hasattr(owner, state) and hasattr(owner, render)]
        rollback = lambda: [render(getattr(owner, state)) for state, render in saved]
    def error(message):
        if not isValid(owner):
            return
        if on_finished:
            on_finished()
        if rollback:
            rollback()
        show_error(owner, message, row)

    def done(result):
        if not isValid(owner):
            return
        if result is False or getattr(result, "ok", True) is False:
            reason = getattr(result, "reason", None)
            if reason == "cancelled":
                if rollback:
                    rollback()
                if on_finished:
                    on_finished()
            else:
                error(reason or "The change could not be applied.")
            return
        if row:
            caption = getattr(row, "helper_text", "")
            row.captionLabel.setText(caption)
            row.captionLabel.setVisible(bool(caption))
        restore = undo
        if old_args is not None:
            restore = lambda: apply_change(owner, fn, *old_args, row=row, **kwargs)
        policy = getattr(result, "ui_priority_policy", None)
        if policy and hasattr(owner, "apply_priority"):
            owner.apply_priority(policy)
        if restart and getattr(result, "restart_required", False):
            owner.request_restart()
        if after:
            after(result)
        if on_finished:
            on_finished()
        if restore and isValid(owner):
            try:
                owner.toast(label, "success", undo=restore)
            except Exception:
                logger.warning("Could not display change notification", exc_info=True)

    call_core(fn, *args, **kwargs, on_result=done, on_error=lambda exc: error(str(exc)))


def bind_numeric(owner, widget, writer, reader, row=None, writer_factory=None):
    """Capture one origin and one Undo for a slider/spin editing transaction."""
    previous = [None]
    target = [writer]

    def begin():
        previous[0] = reader()
        target[0] = writer_factory() if writer_factory else writer
        call_core(owner.core.settings.begin_batch)

    def finish():
        old, fn = previous[0], target[0]
        apply_change(owner, owner.core.settings.end_batch,
                     undo=lambda: apply_change(owner, fn, old, row=row))

    widget.started.connect(begin)
    widget.edited.connect(lambda value: apply_change(owner, target[0], value, row=row))
    widget.finished.connect(finish)
