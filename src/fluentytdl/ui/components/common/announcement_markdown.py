"""Shared read-only Markdown renderer; HTML and external resources are disabled."""

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QTextCursor, QTextDocument
from qfluentwidgets import TextBrowser


class AnnouncementMarkdown(TextBrowser):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_link)

    def setMarkdown(self, text):
        self.document().setMarkdown(
            text,
            QTextDocument.MarkdownFeature.MarkdownDialectGitHub
            | QTextDocument.MarkdownFeature.MarkdownNoHTML,
        )
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.setTextCursor(cursor)
        self.verticalScrollBar().setValue(0)

    def loadResource(self, resource_type, url):
        return None

    @staticmethod
    def _open_link(url: QUrl):
        if url.scheme().lower() in {"https", "http"}:
            QDesktopServices.openUrl(url)
