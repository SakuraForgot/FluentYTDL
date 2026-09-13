from __future__ import annotations

import os

from PySide6.QtCore import QCoreApplication, QLocale, QTranslator
from qfluentwidgets import FluentTranslator

from ..utils.language import normalize_language
from .config_manager import config_manager


class I18nManager:
    """国际化 (i18n) 管理器"""

    _app_translator: QTranslator | None = None
    _fluent_translator: FluentTranslator | None = None

    @classmethod
    def setup_language(cls):
        """初始化全局语言并挂载翻译器，需要在 QApplication 创建后尽早调用"""
        app = QCoreApplication.instance()
        if not app:
            return

        lang_cfg = str(config_manager.get("app_language", "auto")).strip()

        locale = QLocale(normalize_language(lang_cfg, QLocale.system().name()))
        os.environ["FLUENTYTDL_UI_LANGUAGE"] = locale.name()

        QLocale.setDefault(locale)

        # 1. 挂载 qfluentwidgets 自带的翻译器
        if cls._fluent_translator is not None:
            app.removeTranslator(cls._fluent_translator)

        cls._fluent_translator = FluentTranslator(locale)
        app.installTranslator(cls._fluent_translator)

        # 2. 挂载应用自身的翻译器
        if cls._app_translator is not None:
            app.removeTranslator(cls._app_translator)

        cls._app_translator = QTranslator()

        # 本地翻译文件存放路径
        from ..utils.paths import resource_path

        locales_dir = resource_path("assets", "locales")

        # load 规则：前缀为 "fluentytdl"，加上 "_"，加上 locale.name() (如 zh_CN)
        if cls._app_translator.load(locale, "fluentytdl", "_", str(locales_dir)):
            app.installTranslator(cls._app_translator)
