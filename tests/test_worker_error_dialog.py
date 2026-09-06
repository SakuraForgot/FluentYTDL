"""`WorkerErrorDialog` 的按钮收敛契约。

用户截图里那个弹窗（"所选格式不可用"）底下并排挂着「快速获取新的cookie」和
「更新 yt-dlp 并重试」—— 两个都无条件常显，跟这次失败半点关系没有。**给错的动作
比不给动作更糟**：它把一次 SABR 限流引到"重新登录"上去，用户照着做一遍还是失败。

所以本文件锁死"按钮跟着 `fix_action` 走"这件事：cookie 按钮只在失败真和登录态
有关时出现，更新按钮只在工具链确实旧了时出现，其余修复动作走通用按钮、文案取
`recovery_hint`。构造的是真实 `MessageBoxBase`，需要 QApplication，走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-errdlg-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from fluentytdl.diagnostics import diagnose  # noqa: E402
from fluentytdl.ui.components.dialogs.worker_error_dialog import WorkerErrorDialog  # noqa: E402

# ── 夹具：三种真实失败各自的 to_dict() 载荷 ──────────────────

SABR_OUTPUT = "\n".join(
    (
        "WARNING: [youtube] WfAU5IDLbdw: Some tv client https formats have been skipped "
        "as they are missing a url. YouTube is forcing SABR streaming for this client",
        "ERROR: [youtube] WfAU5IDLbdw: Requested format is not available",
    )
)
#: cookie 过期 —— 这一类才该出现「快速获取新的cookie」
AUTH_OUTPUT = "ERROR: [youtube] abc: Sign in to confirm your age"
#: nsig 提取失败 + 403 —— 伴随信号把 fix_action 升级成 update_component
STALE_OUTPUT = "\n".join(
    (
        "WARNING: [youtube] abc: nsig extraction failed: Some formats may be missing",
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    )
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture()
def host(qapp):
    """`MessageBoxBase` 继承的 `MaskDialogBase` 会读 `parent.width()`，父窗口不能为空。

    生产里传的是主窗口；测试只要一个有尺寸的宿主，用裸 `QWidget` 当脚手架
    —— 它不是 UI 组件，不受"必须用 QFluentWidgets"那条约束。
    """
    widget = QWidget()
    widget.resize(900, 700)
    yield widget
    widget.deleteLater()


def _dialog(host: QWidget, output: str, *, phase: str = "") -> WorkerErrorDialog:
    """按真实链路造弹窗：diagnose() → to_dict() → WorkerErrorDialog。"""
    return WorkerErrorDialog(diagnose(1, output, phase=phase).to_dict(), host)


# ── SABR：本次报告的那个弹窗 ─────────────────────────────────


def test_sabr_dialog_shows_pot_button_and_no_cookie_button(host):
    """toolchain 类失败不许再冒出"获取 cookie"，取而代之是那条真能执行的动作。"""
    dlg = _dialog(host, SABR_OUTPUT, phase="parse")

    assert dlg.fix_btn is not None
    assert dlg.fix_btn.text() == "去启用 POT 引擎"
    assert dlg.fetch_cookie_btn is None  # 换 cookie 对 SABR 毫无作用
    assert dlg.update_ytdlp_btn is None  # 事件流里没有工具链过旧的信号


def test_sabr_dialog_text_carries_the_corrected_verdict(host):
    """标题/正文取自 catalog，"换档位没有用"必须出现在用户眼前。"""
    dlg = _dialog(host, SABR_OUTPUT, phase="parse")

    assert "YouTube" in dlg.title_label.text()
    assert "换档位没有用" in dlg.content_label.text()


def test_dialog_detail_box_states_the_phase(host):
    """详情框首行说明死在哪一步 —— 用户不必再从任务卡上猜。"""
    dlg = _dialog(host, SABR_OUTPUT, phase="parse")

    assert dlg.tech_edit.toPlainText().startswith("exit_code=1 phase=parse")


def test_fix_button_emits_the_action_verbatim(host):
    """点通用按钮 ⇒ 原样发出 fix_action，主窗口据此调 execute_fix_action。"""
    dlg = _dialog(host, SABR_OUTPUT)
    seen: list[str] = []
    dlg.fix_requested.connect(seen.append)

    dlg.fix_btn.click()

    assert seen == ["enable_pot_provider"]


# ── 对照组：cookie / 工具链两类失败按钮不变 ──────────────────


def test_auth_failure_still_offers_the_cookie_button(host):
    """收敛不能把该给的动作也收掉：登录态类失败照旧给"获取 cookie"。"""
    dlg = _dialog(host, AUTH_OUTPUT)

    assert dlg.err_data["category"] == "auth"
    assert dlg.fetch_cookie_btn is not None


def test_stale_toolchain_offers_the_update_button(host):
    """nsig 失败伴随 403 ⇒ 首选动作是更新组件，按钮必须在。"""
    dlg = _dialog(host, STALE_OUTPUT)

    assert dlg.err_data["fix_action"] == "update_component"
    assert dlg.update_ytdlp_btn is not None
    assert dlg.fix_btn is None  # update_component 有专属按钮，不再重复一个通用的


def test_no_fix_action_means_no_extra_buttons(host):
    """`fix_action` 为空（用户真挑错档位）⇒ 一个快捷修复按钮都不该有。"""
    dlg = _dialog(host, "ERROR: [youtube] abc: Requested format is not available")

    assert dlg.err_data["fix_action"] is None
    assert dlg.fix_btn is None
    assert dlg.fetch_cookie_btn is None
    assert dlg.update_ytdlp_btn is None
    assert dlg.settings_btn is not None  # "去设置页排查"是兜底出口，永远保留


def test_dialog_accepts_translate_error_shaped_events(host):
    """`translate_error()` 那条路给的 `events` 是纯字符串列表，也要认。"""
    dlg = WorkerErrorDialog(
        {
            "user_title": "发生未知错误",
            "user_message": "…",
            "events": ["nsig_extraction_failed"],
        },
        host,
    )

    assert dlg.update_ytdlp_btn is not None
