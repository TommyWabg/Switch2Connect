"""Profile checklists, asynchronous file pickers and combo capture."""
import threading
from PySide6.QtWidgets import QFileDialog, QWidget, QVBoxLayout, QScrollArea
from qfluentwidgets import MessageBoxBase, BodyLabel, CheckBox
from ...qt_bridge import call_core, gui_dispatcher


def pick_file(page, title, callback, *, save=False, apps=False):
    """Enter/exit the service's blocking focus guard away from the GUI thread."""
    dispatch = gui_dispatcher()
    closed = threading.Event()
    page.destroyed.connect(closed.set)
    def show():
        if closed.is_set(): return
        dialog = QFileDialog(page.window(), title)
        dialog.setNameFilter('Applications (*.exe)' if apps else 'YAML profiles (*.yaml *.yml)')
        dialog.setAcceptMode(QFileDialog.AcceptSave if save else QFileDialog.AcceptOpen)
        dialog.setFileMode(QFileDialog.AnyFile if save else QFileDialog.ExistingFile)
        if save:
            dialog.setDefaultSuffix('yaml'); dialog.selectFile('profiles.yaml')
        if apps:
            call_core(lambda: page.core.profiles.default_app_dir,
                      on_result=lambda directory: dialog.setDirectory(str(directory)))
        page.file_dialog = dialog
        def finished(accepted):
            files = dialog.selectedFiles()
            closed.set()
            if accepted and files:
                path = files[0]
                if save and not path.lower().endswith('.yaml'):
                    path += '.yaml'
                callback(path)
        dialog.finished.connect(finished)
        dialog.open()
    def worker():
        try:
            with page.core.profiles.suspend_focus_polling():
                dispatch.requested.emit(show)
                closed.wait()
        except Exception as exc:
            message = str(exc)
            dispatch.requested.emit(lambda: page.toast(message, 'error'))
    threading.Thread(target=worker, daemon=True).start()


class ChecklistDialog(MessageBoxBase):
    def __init__(self, preview, importing, parent=None):
        super().__init__(parent)
        self.viewLayout.addWidget(BodyLabel('Import profiles' if importing else 'Export profiles', self))
        self.select_all = CheckBox('Select all', self); self.select_all.setChecked(True)
        self.viewLayout.addWidget(self.select_all)
        area = QScrollArea(self); area.setWidgetResizable(True); area.setMinimumHeight(160)
        content = QWidget(area); layout = QVBoxLayout(content); area.setWidget(content)
        self.checks = {}
        for name in preview.names:
            check = CheckBox(name, content); check.setToolTip(name); check.setChecked(True)
            self.checks[name] = check; layout.addWidget(check)
            check.stateChanged.connect(self.validate)
        layout.addStretch(); self.viewLayout.addWidget(area)
        self.keep = CheckBox('Keep current profiles', self); self.keep.setChecked(True)
        self.keep.setVisible(importing); self.viewLayout.addWidget(self.keep)
        self.controller = CheckBox(('Import' if importing else 'Export') + ' controller related data', self)
        self.controller.setVisible(preview.has_controller_data); self.viewLayout.addWidget(self.controller)
        self.controller.stateChanged.connect(self.validate)
        self.select_all.stateChanged.connect(lambda: [c.setChecked(self.select_all.isChecked()) for c in self.checks.values()])
        self.yesButton.setText('Import' if importing else 'Export'); self.cancelButton.setText('Cancel')
        self.error = BodyLabel('', self); self.viewLayout.addWidget(self.error)
        self.widget.setMinimumWidth(440); self.validate()

    def selected(self):
        return tuple(name for name, check in self.checks.items() if check.isChecked())

    def validate(self, *_):
        valid = bool(self.selected()) or self.controller.isChecked()
        self.yesButton.setEnabled(valid)
        self.error.setText('' if valid else 'Select a profile or controller related data.')
        return valid


