from PySide6.QtCore import Signal
from qfluentwidgets import (
    BodyLabel,
    ComboBox,
    ExpandGroupSettingCard,
    FluentIcon,
    PushButton,
    PushSettingCard,
)


class PlatformAuthExpandCard(ExpandGroupSettingCard):
    """多平台 WebView2 账号认证手风琴卡片"""

    # 信号定义
    loginClicked = Signal(str)  # platform
    accountChanged = Signal(str, str)  # platform, account_id
    addAccountClicked = Signal(str)  # platform
    removeAccountClicked = Signal(str)  # platform
    refreshCookieClicked = Signal(str)  # platform
    openCookieLocationClicked = Signal(str)  # platform

    def __init__(self, platform: str, icon, title: str, content: str, parent=None):
        super().__init__(icon, title, content, parent)
        self.platform = platform
        self._account_ids: list[str] = []

        # -- Header 自定义组件 --

        # 当前账号标签
        self.accountLabel = BodyLabel(self.tr("当前账号:"), self)

        # 账号下拉框
        self.accountComboBox = ComboBox(self)
        self.accountComboBox.setMinimumWidth(150)
        self.accountComboBox.currentIndexChanged.connect(self._on_account_changed)

        # 登录按钮
        self.loginButton = PushButton(self.tr("点击登录"), self)
        self.loginButton.clicked.connect(lambda: self.loginClicked.emit(self.platform))

        # 添加到头部 (按顺序添加到尾部，在 expand button 之前)
        self.addWidget(self.accountLabel)
        self.addWidget(self.accountComboBox)
        self.addWidget(self.loginButton)

        # -- 内部卡片组件 (手风琴展开内容) --
        self.addAccountCard = PushSettingCard(
            self.tr("新增账号"),
            FluentIcon.ADD,
            self.tr("新增 WebView2 账号"),
            self.tr("创建新的 {0} 隔离存储账号").format(title),
        )
        self.addAccountCard.clicked.connect(lambda: self.addAccountClicked.emit(self.platform))

        self.removeAccountCard = PushSettingCard(
            self.tr("删除当前账号"),
            FluentIcon.DELETE,
            self.tr("删除当前账号"),
            self.tr("删除当前选中的 WebView2 账号（至少保留 1 个）"),
        )
        self.removeAccountCard.clicked.connect(
            lambda: self.removeAccountClicked.emit(self.platform)
        )

        self.refreshCookieCard = PushSettingCard(
            self.tr("立即刷新"),
            FluentIcon.SYNC,
            self.tr("手动刷新 Cookie"),
            self.tr("从浏览器重新提取 Cookie（可能需要管理员权限）"),
        )
        self.refreshCookieCard.clicked.connect(
            lambda: self.refreshCookieClicked.emit(self.platform)
        )

        self.cookieStatusCard = PushSettingCard(
            self.tr("打开位置"),
            FluentIcon.INFO,
            self.tr("Cookie 状态检测"),
            self.tr("显示当前关联的 Cookie 存活状态"),
        )
        self.cookieStatusCard.clicked.connect(
            lambda: self.openCookieLocationClicked.emit(self.platform)
        )

        self.addGroupWidget(self.addAccountCard)
        self.addGroupWidget(self.removeAccountCard)
        self.addGroupWidget(self.refreshCookieCard)
        self.addGroupWidget(self.cookieStatusCard)

        self.reload_accounts()

    def reload_accounts(self, select_current: bool = True) -> None:
        """刷新当前平台下的 WebView2 账号列表"""
        from ....auth.auth_service import auth_service

        accounts = auth_service.list_webview2_accounts(platform=self.platform)
        self._account_ids = [a.account_id for a in accounts]

        self.accountComboBox.blockSignals(True)
        self.accountComboBox.clear()

        for acc in accounts:
            label = acc.localized_name
            if acc.is_default:
                label += self.tr(" (默认)")
            self.accountComboBox.addItem(label)

        if select_current and self._account_ids:
            cur = auth_service.get_current_webview2_account_id(self.platform)
            idx = self._account_ids.index(cur) if cur in self._account_ids else 0
            self.accountComboBox.setCurrentIndex(idx)

        self.accountComboBox.blockSignals(False)

    def _on_account_changed(self, index: int) -> None:
        """切换账号触发信号"""
        if index < 0 or index >= len(self._account_ids):
            return
        account_id = self._account_ids[index]
        self.accountChanged.emit(self.platform, account_id)

    def set_login_button_enabled(self, enabled: bool) -> None:
        self.loginButton.setEnabled(enabled)

    def set_content(self, content: str) -> None:
        if hasattr(self, "card") and hasattr(self.card, "setContent"):
            self.card.setContent(content)
