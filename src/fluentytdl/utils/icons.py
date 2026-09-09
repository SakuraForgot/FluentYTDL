from __future__ import annotations

from PySide6.QtGui import QIcon

from .paths import resource_path

# 多尺寸图标的首选来源：FluentYTDL_v2.ico 内含 16/20/24/32/48/64/72/96/128/256 共 10 帧，
# 由 assets/logo.svg 经 scripts/generate_app_icons.py 生成。
# 且与 EXE 资源图标（任务栏、资源管理器、快捷方式）是同一份素材，
# 用它能保证托盘图标和 shell 图标外观完全一致。
# Windows 通知区在 100% / 125% / 150% DPI 下分别索取 16 / 20 / 24 px，
# 只提供一张 256px 大图会被 Qt 缩成一团糊，因此必须提供带小尺寸帧的素材。
_ICON_PRIMARY = "FluentYTDL_v2.ico"

# .ico 缺失时的退路：与 .ico 同一素材族（同样带留白）的分尺寸 PNG。
# 注意不要把 logo_tight.png 混进来——那是裁掉留白的另一套素材，
# 混用会让图标在 128px 与 256px 之间出现明显的大小跳变。
_ICON_PNG_FALLBACKS = (
    "logo_16.png",
    "logo_32.png",
    "logo_64.png",
    "logo_128.png",
    "logo.png",  # 256px
)

# 两套素材都拿不到时的最后兜底（裁剪变体，仅单一 256px 尺寸）。
_ICON_LAST_RESORT = "logo_tight.png"


def load_app_icon() -> QIcon:
    """加载多尺寸应用图标（窗口 / 任务栏 / 系统托盘共用）。

    资源缺失时返回空 QIcon（`isNull()` 为真），由调用方决定如何降级——
    不要在这里伪造一个纯色图标，那会把「没有图标」变成「坏图标」。
    """
    primary = resource_path("assets", _ICON_PRIMARY)
    if primary.exists():
        icon = QIcon(str(primary))
        if not icon.isNull():
            return icon

    icon = QIcon()
    for name in _ICON_PNG_FALLBACKS:
        path = resource_path("assets", name)
        if path.exists():
            icon.addFile(str(path))

    if icon.isNull():
        last = resource_path("assets", _ICON_LAST_RESORT)
        if last.exists():
            icon = QIcon(str(last))

    return icon
