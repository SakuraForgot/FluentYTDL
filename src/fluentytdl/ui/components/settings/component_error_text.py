"""把组件更新链路的**稳定错误码**翻成界面文案。

为什么要有这么一层：`download_error` / `check_error` 信号里流通的是稳定 code
（`component_update_url_unresolved` 之类），因为同一个字符串会被写进 JSONL 事件日志，
而本地化文案随界面语言变化，写进日志就没法跨语言 grep（CLAUDE.md §5）。

所以翻译只能发生在最靠 UI 的一侧，且必须对**未知 code 原样透出** —— worker 里的
兜底路径会把 `type(e).__name__: 详情` 塞进 detail，而 code 本身仍是已知的那几个；
真出现没见过的 code 时显示原文比显示"未知错误"有用得多。
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication


def QT_TRANSLATE_NOOP(_context: str, text: str) -> str:
    """标记待提取的翻译源串，原样返回。

    `pyside6-lupdate` 是按**调用点的函数名 + 字面量实参**扫描提取的，
    `translate(code)` 里那句 `QCoreApplication.translate("ComponentError", text)`
    传的是变量，一条都抽不出来（`tests/test_i18n_integrity.py` 会因此报
    "context 'ComponentError' 在 .ts 里没有它"）。用这个恒等标记把源串留在字面量
    位置，抽取就能扫到，运行期的实际翻译仍然发生在 `translate()` 里。

    与 `diagnostics/catalog.py` 同款写法（那里也是 dict 里存源串、取用时才翻）。
    """
    return text


#: code → 未翻译的中文原文。真正的翻译在 `translate()` 里过 `QCoreApplication.translate`，
#: 这里只用 `QT_TRANSLATE_NOOP` 标记源串 —— 模块级直接调 `tr()` 会在翻译器装好之前求值。
_MESSAGES: dict[str, str] = {
    # dependency_manager
    "component_update_url_unresolved": QT_TRANSLATE_NOOP(
        "ComponentError", "没能解析出下载地址，请检查网络或稍后重试"
    ),
    "component_worker_start_failed": QT_TRANSLATE_NOOP("ComponentError", "无法启动更新进程"),
    "component_worker_crashed": QT_TRANSLATE_NOOP("ComponentError", "更新进程异常退出"),
    "component_worker_exit_nonzero": QT_TRANSLATE_NOOP("ComponentError", "更新失败，详情见日志"),
    "component_download_stalled": QT_TRANSLATE_NOOP("ComponentError", "下载长时间无响应，已中止"),
    # updater_worker
    "worker_bad_input": QT_TRANSLATE_NOOP("ComponentError", "更新进程收到了无效参数"),
    "worker_missing_params": QT_TRANSLATE_NOOP("ComponentError", "更新进程缺少必要参数"),
    "worker_hash_mismatch": QT_TRANSLATE_NOOP(
        "ComponentError", "文件校验失败，下载内容可能被篡改或损坏"
    ),
    "worker_download_failed": QT_TRANSLATE_NOOP("ComponentError", "下载失败，请检查网络或代理设置"),
    "component_worker_error": QT_TRANSLATE_NOOP("ComponentError", "更新失败，详情见日志"),
}


def translate(code: str) -> str:
    """已知 code 翻成中文，未知 code 原样返回。"""
    text = _MESSAGES.get(code)
    if text is None:
        return code
    return QCoreApplication.translate("ComponentError", text)
