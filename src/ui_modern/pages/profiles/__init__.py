from ...widgets.changes import apply_change
from PySide6.QtWidgets import QWidget, QHBoxLayout
from PySide6.QtGui import QAction
from PySide6.QtCore import Qt
from qfluentwidgets import PushButton, TransparentPushButton, SwitchButton, RoundMenu, MessageBoxBase, MessageBox, LineEdit, BodyLabel, themeColor, isDarkTheme, qconfig
from ...widgets.display import input_display
from ...qt_bridge import call_core
from ...widgets.common import SettingsGroup, Segments
from ..rumble.support import ServicePage, sync
from .dialogs import ChecklistDialog, pick_file
from ...widgets.combo_recording import ComboRecorder


class ProfilesPage(ServicePage):
    def __init__(self, core, toast, parent=None):
        super().__init__('Profiles', core, toast, parent)
        self.profile_rows, self.app_rows = {}, {}
        actions = QWidget(self.content); layout = QHBoxLayout(actions); layout.setContentsMargins(0, 0, 0, 0)
        for title, callback in [('New profile', lambda: apply_change(self, core.profiles.add)), ('Import', self.import_profiles), ('Export', self.export_profiles)]:
            button = PushButton(title, actions); button.clicked.connect(callback); layout.addWidget(button)
        layout.addStretch(); self.box.addWidget(actions)
        self.list = SettingsGroup('Profiles', self.content); self.box.addWidget(self.list)
        self.apps = SettingsGroup('Assigned apps', self.content); self.box.addWidget(self.apps)
        self.add_app = PushButton('Add app…', self.apps); self.apps.heading.addWidget(self.add_app)
        self.add_app.clicked.connect(lambda: self.choose_app())
        self.switching = SettingsGroup('Profile switching', self.content); self.box.addWidget(self.switching)
        holder = QWidget(self.switching); line = QHBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0)
        self.trigger = PushButton('None', holder)
        self.clear_trigger = PushButton('Clear', holder)
        line.addWidget(self.trigger); line.addWidget(self.clear_trigger)
        self.switching.row('Global combo trigger', holder)
        self.trigger.clicked.connect(lambda: self.record())
        self.clear_trigger.clicked.connect(lambda: apply_change(self, core.profiles.clear_profile_switching_combo_trigger,
            undo=lambda old=self.state.combo_trigger: apply_change(self, core.profiles.set_profile_switching_combo_trigger, old)))
        self.mode = Segments(self.switching)
        self.switching.row('Change profile mode', self.mode)
        self.mode.currentItemChanged.connect(lambda val: apply_change(self, core.profiles.set_change_profile_mode, val,
            undo=lambda old=self.state.change_profile_mode: apply_change(self, core.profiles.set_change_profile_mode, old)))
        self.box.addStretch(); self.bridge.profiles_changed.connect(self.apply_profiles)
        qconfig.themeChangedFinished.connect(self.refresh_profiles_theme)
        call_core(core.profiles.state, on_result=self.apply_profiles)

    def refresh_profiles_theme(self):
        if hasattr(self, 'state'):
            self.apply_profiles(self.state)

    def apply_profiles(self, state):
        self.state = state
        combos = dict(state.switching_combos)
        for name in state.names:
            if name not in self.profile_rows:
                holder = QWidget(self.list); layout = QHBoxLayout(holder); layout.setContentsMargins(0, 0, 0, 0)
                include = SwitchButton(holder); include.setOnText(''); include.setOffText(''); include.setFocusPolicy(Qt.StrongFocus); include.setAccessibleName('Include ' + name + ' in change list')
                include.setToolTip('Include in change list')
                menu = PushButton('⋯', holder); menu.setAccessibleName('Actions for ' + name)
                layout.addWidget(BodyLabel("Include in change list", holder))
                for control in (include, menu): layout.addWidget(control)
                row = self.list.row(name, holder, 'No switching combo')
                select = TransparentPushButton(name, row); row.textLayout.insertWidget(0, select)
                row.textLayout.setAlignment(select, Qt.AlignLeft)
                row.titleLabel.hide()
                select.clicked.connect(lambda checked=False, n=name: self.select_profile(n))
                include.checkedChanged.connect(lambda val, n=name: self.include(n, val))
                menu.clicked.connect(lambda checked=False, n=name, b=menu: self.profile_menu(n, b))
                self.profile_rows[name] = row, select, include, menu
            row, select, include, menu = self.profile_rows[name]
            select.setStyleSheet("QPushButton { background: transparent; border: none; text-align: left; padding: 0; " +
                ("color: " + (themeColor().name() if name == state.active else "#ffffff" if isDarkTheme() else "#202020") + ";") + " }")
            row.show(); select.setText(('● ' if name == state.active else '') + name); select.setToolTip(name)
            sync(include, 'setChecked', name in state.change_list)
            row.captionLabel.setText(input_display(combos.get(name)) if combos.get(name) else 'No switching combo')
        for name, controls in self.profile_rows.items():
            controls[0].setVisible(name in state.names)
        self.trigger.setText(input_display(state.combo_trigger) if state.combo_trigger else 'None'); self.trigger.setToolTip(self.trigger.text())
        self.clear_trigger.setEnabled(bool(state.combo_trigger))
        self.mode.sync(('Auto', 'Manual'), state.change_profile_mode)
        apps = dict(state.assigned_apps).get(state.active, ())
        self.apps.label.setText('Assigned apps · ' + state.active)
        for index, (path, display) in enumerate(apps):
            if index not in self.app_rows:
                holder = QWidget(self.apps); layout = QHBoxLayout(holder); layout.setContentsMargins(0, 0, 0, 0)
                choose = PushButton('Choose app…', holder); remove = PushButton('Remove', holder)
                layout.addWidget(choose); layout.addWidget(remove)
                row = self.apps.row('', holder); self.app_rows[index] = row
                choose.clicked.connect(lambda checked=False, i=index: self.choose_app(i))
                remove.clicked.connect(lambda checked=False, i=index: self.remove_app(i))
            row = self.app_rows[index]; row.titleLabel.setText(display); row.titleLabel.setToolTip(path); row.show()
        for index, row in self.app_rows.items(): row.setVisible(index < len(apps))

    def include(self, name, included):
        old = name in self.state.change_list
        apply_change(self, self.core.profiles.set_in_change_list, name, included,
                     undo=lambda: apply_change(self, self.core.profiles.set_in_change_list, name, old))

    def clear_combo(self, name):
        old = dict(self.state.switching_combos).get(name, '')
        apply_change(self, self.core.profiles.clear_profile_combo, name,
                     undo=lambda: apply_change(self, self.core.profiles.set_profile_combo, name, old))

    def record(self, name=None):
        save = (lambda value: self.core.profiles.set_profile_combo(name, value)) if name else self.core.profiles.set_profile_switching_combo_trigger
        old = dict(self.state.switching_combos).get(name, '') if name else self.state.combo_trigger
        self.recorder = ComboRecorder(self.core, save, self.window(),
            on_saved=lambda value: self.toast('Combo saved', 'success', undo=lambda: apply_change(self, save, old)))
        self.recorder.show()

    def select_profile(self, name):
        old = self.state.active
        if name != old:
            apply_change(self, self.core.profiles.select, name, undo=lambda: apply_change(self, self.core.profiles.select, old))

    def profile_menu(self, name, button):
        menu = RoundMenu(parent=self)
        for label, callback in [('Record combo…', lambda: self.record(name)), ('Clear combo', lambda: self.clear_combo(name)), ('Rename', lambda: self.rename(name)), ('Reset', lambda: self.active_action(name, self.core.profiles.reset)), ('Delete', lambda: self.active_action(name, self.core.profiles.delete))]:
            action = QAction(label, menu); action.triggered.connect(callback)
            if label == 'Clear combo': action.setEnabled(bool(dict(self.state.switching_combos).get(name)))
            if label == 'Delete': action.setEnabled(len(self.state.names) > 1)
            menu.addAction(action)
        self.menu = menu; menu.exec(button.mapToGlobal(button.rect().bottomLeft()))

    def active_action(self, name, action):
        if name == self.state.active:
            apply_change(self, action)
        else:
            apply_change(self, self.core.profiles.select, name, after=lambda result: apply_change(self, action) if result.ok else None)

    def rename(self, name):
        dialog = MessageBoxBase(self.window()); dialog.viewLayout.addWidget(BodyLabel('Rename profile', dialog))
        field = LineEdit(dialog); field.setText(name); field.selectAll(); dialog.viewLayout.addWidget(field)
        error = BodyLabel('', dialog); dialog.viewLayout.addWidget(error)
        def validate(text):
            valid = bool(text.strip()) and (text == name or text not in self.state.names)
            error.setText('' if valid else 'Enter a unique profile name.')
            dialog.yesButton.setEnabled(valid)
        field.textChanged.connect(validate)
        field.returnPressed.connect(dialog.yesButton.click)
        def finished(accepted):
            if accepted and field.text() != name:
                new_name = field.text()
                def rename():
                    apply_change(self, self.core.profiles.rename, new_name,
                        undo=lambda: self.active_action(new_name, lambda: self.core.profiles.rename(name)))
                self.active_action(name, rename)
        dialog.finished.connect(finished)
        self.rename_dialog = dialog
        dialog.show()
        field.setFocus()

    def choose_app(self, index=None):
        name = self.state.active
        previous = dict(self.state.assigned_apps).get(name, ())
        pick_file(self, 'Choose app', lambda path: apply_change(self, self.core.profiles.add_assigned_app, path, index=index, profile_name=name,
            undo=lambda: apply_change(self, self.core.profiles.set_profile_assigned_apps, name, [{'path': p, 'name': n} for p, n in previous])), apps=True)

    def remove_app(self, index):
        name = self.state.active
        previous = dict(self.state.assigned_apps).get(name, ())
        apply_change(self, self.core.profiles.remove_assigned_app, index, profile_name=name,
                     undo=lambda: apply_change(self, self.core.profiles.set_profile_assigned_apps, name, [{'path': p, 'name': n} for p, n in previous]))

    def import_profiles(self):
        pick_file(self, 'Import profiles', lambda path: call_core(self.core.profiles.inspect_import, path,
            on_result=self.show_import, on_error=lambda exc: self.toast(str(exc), 'error')))

    def show_import(self, preview):
        if not preview.ok:
            self.toast(preview.reason or 'Cannot read profiles.', 'error')
            return
        dialog = ChecklistDialog(preview, True, self.window())
        self.checklist = dialog
        def finished(accepted):
            if not accepted: return
            selected, keep, controller = dialog.selected(), dialog.keep.isChecked(), dialog.controller.isChecked()
            def apply(): apply_change(self, self.core.profiles.do_import, preview.path, selected, keep, controller)
            if selected and not keep:
                confirmation = MessageBox('Replace profiles', 'Replace all current profiles with the selected imported profiles?', self.window())
                confirmation.yesButton.setText('Replace')
                confirmation.cancelButton.setText('Cancel')
                confirmation.finished.connect(lambda accepted: apply() if accepted else None)
                self.import_confirmation = confirmation
                confirmation.show()
            else: apply()
        dialog.finished.connect(finished)
        dialog.show()

    def export_profiles(self):
        call_core(self.core.profiles.export_preview, on_result=self.show_export,
                  on_error=lambda exc: self.toast(str(exc), 'error'))

    def show_export(self, preview):
        dialog = ChecklistDialog(preview, False, self.window())
        self.checklist = dialog
        def finished(accepted):
            if accepted:
                selected, controller = dialog.selected(), dialog.controller.isChecked()
                pick_file(self, 'Export profiles', lambda path: apply_change(self, self.core.profiles.export, path, selected, controller), save=True)
        dialog.finished.connect(finished)
        dialog.show()


def create_page(core, toast, parent=None):
    return ProfilesPage(core, toast, parent)
