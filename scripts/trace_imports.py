import importlib
import sys
import traceback
from pathlib import Path

# 按 __file__ 解析，不写死盘符：这里原先硬编码 "E:/YouTube/FluentYTDL/src"，
# 换一台机器（或换个盘）就整个脚本空转。
root_dir = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(root_dir))

# 这几个都是 import 期最容易出事的重型 UI 模块。路径要跟着重构走 —— 上面四条
# 曾长期停留在 ui/components/ 的旧布局，import 失败被 except 吞掉，看着像"跑过了"。
modules = [
    "fluentytdl.ui.reimagined_main_window",
    "fluentytdl.ui.settings_page",
    "fluentytdl.ui.components.dialogs.download_config_window",
    "fluentytdl.ui.components.common.rate_limit",
    "fluentytdl.ui.components.settings.smart_setting_card",
    "fluentytdl.ui.dialogs.playlist_subtitle_dialog",
]

for mod in modules:
    try:
        importlib.import_module(mod)
        print(f"Successfully imported {mod}")
    except Exception:
        print(f"\n--- {mod} ---")
        traceback.print_exc()
