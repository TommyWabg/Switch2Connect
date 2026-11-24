"""Translations for the modern UI. English source strings are the fallback."""
import json
import logging

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from qfluentwidgets import FluentTranslator

from . import theme
from .resources import resource_path

LANGUAGES = {"system": "Follow system", "en": "English", "zh_CN": "简体中文"}
_catalog = {}


def preference():
    code = theme.preferences().value("language", "system")
    return code if code in LANGUAGES else "system"


def resolve_language(code, system_languages):
    if code in LANGUAGES and code != "system":
        return code
    for tag in system_languages:
        locale = QLocale(tag)
        if locale.language() == QLocale.Language.Chinese:
            return "zh_CN"
        if locale.language() == QLocale.Language.English:
            return "en"
    return "en"


def initialize(app):
    global _catalog
    code = resolve_language(preference(), QLocale.system().uiLanguages())
    _catalog = {}
    for translator in getattr(app, "_ui_translators", ()):
        app.removeTranslator(translator)
    app._ui_translators = []
    if code == "en":
        return
    try:
        _catalog = json.loads(resource_path(f"resources/i18n/{code}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logging.getLogger(__name__).exception("Could not load UI language %s", code)
        return
    qt = QTranslator()
    if qt.load(f"qtbase_{code}", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app._ui_translators.append(qt)
    app._ui_translators.append(FluentTranslator(QLocale(code)))
    for translator in app._ui_translators:
        app.installTranslator(translator)


def tr(source, **values):
    text = _catalog.get(source, source)
    return text.format(**values) if values else text
