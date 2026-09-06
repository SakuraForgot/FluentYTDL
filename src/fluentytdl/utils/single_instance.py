from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from qfluentwidgets import MessageBox


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
        """显示程序已在运行的提示弹窗"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication, QWidget

        # 创建一个透明无边框的占位窗口作为 MessageBox 的 parent
        # 因为 qfluentwidgets 的 MessageBox 继承自 MaskDialogBase，需要获取 parent 的尺寸
        dummy = QWidget()
        dummy.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        dummy.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        dummy.resize(500, 300)

        # 将 dummy 居中显示在主屏幕上
        app = QApplication.instance()
        if app:
            screen_geometry = app.primaryScreen().geometry()
            dummy.move(
                screen_geometry.width() // 2 - dummy.width() // 2,
                screen_geometry.height() // 2 - dummy.height() // 2,
            )

        dummy.show()

        w = MessageBox(self.tr("提示"), self.tr("FluentYTDL 已经在运行中。"), dummy)
        w.yesButton.setText(self.tr("确定"))
        w.cancelButton.hide()
        w.exec()

        # 弹窗结束后清理
        dummy.close()
        dummy.deleteLater()
