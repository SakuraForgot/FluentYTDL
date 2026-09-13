import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QSignalSpy, QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets import FluentWindow  # noqa: E402

from fluentytdl.ui.announcement_controller import AnnouncementDialog  # noqa: E402


def test_mandatory_dialog_requires_explicit_acknowledgement():
    app = QApplication.instance() or QApplication([])
    parent = FluentWindow()
    parent.resize(900, 700)
    parent.show()
    dialog = AnnouncementDialog({"title": "Notice", "body": "## Heading\n\n**Text**"}, parent, True)
    spy = QSignalSpy(dialog.accepted)
    dialog.show()
    app.processEvents()

    dialog.reject()
    assert dialog.isVisible() and spy.count() == 0
    dialog.yesButton.click()
    QTest.qWait(400)
    assert spy.count() == 1
    dialog.deleteLater()
    parent.hide()
    parent.deleteLater()
    app.processEvents()


def test_long_announcement_fits_small_parent_and_starts_at_top():
    from fluentytdl.ui.components.common.announcement_markdown import AnnouncementMarkdown

    app = QApplication.instance() or QApplication([])
    parent = FluentWindow()
    parent.resize(520, 460)
    parent.show()
    dialog = AnnouncementDialog(
        {
            "title": "Important component update information with a longer title",
            "body": "## Start here\n\n" + "Long paragraph with update instructions.\n\n" * 50,
        },
        parent,
    )
    dialog.show()
    QTest.qWait(350)
    try:
        assert dialog.widget.width() <= parent.width() - 32
        assert dialog.widget.height() <= parent.height() - 32
        assert dialog.yesButton.isVisible()
        content = dialog.findChild(AnnouncementMarkdown)
        assert content.verticalScrollBar().maximum() > 0
        assert content.verticalScrollBar().value() == 0
    finally:
        dialog.hide()
        dialog.deleteLater()
        parent.hide()
        parent.deleteLater()
        app.processEvents()
