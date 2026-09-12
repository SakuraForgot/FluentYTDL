from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from qfluentwidgets import Dialog


class SingleInstanceChecker(QObject):
    """单实例检测器

    基于 QLocalServer/QLocalSocket 实现。
    当发现已经存在实例时，向主实例发送信号并弹窗阻止当前实例启动。
    当作为主实例时，监听并提供 new_instance_detected 信号以供唤醒主界面。
    """

    # 当作为服务端（主实例）时，检测到新实例启动时发出此信号
    new_instance_detected = Signal()

    def __init__(self, server_name: str, parent=None):
        super().__init__(parent)
        self.server_name = server_name
        self.server = None

    def check_and_start(self) -> bool:
        """检查单实例

        Returns:
            bool: 如果是首个实例并成功启动监听返回 True；如果已有实例运行返回 False。
        """
        socket = QLocalSocket()
        socket.connectToServer(self.server_name)

        if socket.waitForConnected(500):
            # 已经有实例在运行，发送唤醒信号给主实例
            socket.write(b"WAKE_UP")
            socket.waitForBytesWritten(500)
            socket.disconnectFromServer()

            # 在当前（新）实例中弹窗提示
            self._show_already_running_message()
            return False

        # 如果没有实例在运行（或者之前的实例意外退出导致残留）
        # 先清理同名的残余 server
        QLocalServer.removeServer(self.server_name)

        # 启动监听作为主实例
        self.server = QLocalServer(self)
        self.server.listen(self.server_name)
        self.server.newConnection.connect(self._on_new_connection)

        return True

    def _on_new_connection(self):
        """处理新实例的连接请求"""
        socket = self.server.nextPendingConnection()
        if socket:
            socket.waitForReadyRead(500)
            socket.readAll()  # 读取内容（忽略具体内容，只需知道有人连接）
            socket.disconnectFromServer()
            socket.deleteLater()

        # 发送信号，让主程序决定如何处理（例如窗口置顶）
        self.new_instance_detected.emit()

    def _show_already_running_message(self):
        """Standalone Fluent dialog: no transparent host or modal mask rectangle."""
        import json

        from PySide6.QtCore import QLocale, QTranslator
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication
        from qfluentwidgets import Theme, setTheme

        from .icons import load_app_icon
        from .language import normalize_language
        from .paths import config_path, resource_path

        # Read only: the duplicate must not initialize ConfigManager or migrate data.
        try:
            settings = json.loads(config_path().read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        modes = {"Light": Theme.LIGHT, "Dark": Theme.DARK}
        setTheme(modes.get(settings.get("theme_mode"), Theme.AUTO))
        app = QApplication.instance()
        app.setFont(QFont("Microsoft YaHei UI", 9))
        app.setWindowIcon(load_app_icon())
        locale = normalize_language(settings.get("app_language", "auto"), QLocale.system().name())
        translator = QTranslator()
        translator.load(str(resource_path("assets", "locales", f"fluentytdl_{locale}.qm")))
        app.installTranslator(translator)
        try:
            dialog = Dialog(self.tr("提示"), self.tr("FluentYTDL 已经在运行中。"))
            dialog.setWindowTitle(self.tr("提示"))
            dialog.yesButton.setText(self.tr("确定"))
            dialog.cancelButton.hide()
            dialog.adjustSize()
            screen = app.primaryScreen()
            if screen:
                dialog.move(screen.availableGeometry().center() - dialog.rect().center())
            dialog.exec()
            dialog.deleteLater()
        finally:
            app.removeTranslator(translator)
