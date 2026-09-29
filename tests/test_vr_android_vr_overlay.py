"""VR 通道在 SABR 核心下的 android_vr 覆盖层契约（Groups A/B/C/D）。

锁死四件事，全部**按协议/能力驱动，不看频道名**：
1. `apply_android_vr_overlay` 只覆盖 `youtube.player_client=android_vr`，*保留* POT Provider
   的独立 ie_key（`youtubepot-bgutilhttp`）与 youtube 命名空间的兄弟键，并剥离 cookiefile；
2. 覆盖后转 CLI，argv 落 `--extractor-args youtube:...player_client=android_vr` 且不含 `--cookies`；
3. `_build_vr_ydl_options` = build_ydl_options + skip_download + 覆盖层（解析==下载同一事实源）；
4. `_probe_vr_capability` 的协议分布 / has_muxed / has_https_direct / has_sabr；
5. 下载端客户端决策**按能力分叉**：官方核心（无 sabr）照旧重钉 android_vr；本核心把
   android_vr 变成 SABR 发射器（has_sabr）且签不出它的 GVS POT 时改用默认客户端集，
   并给选择器挂 `bv*+ba/b` 兜底——唯独选中 android_vr 独占 ID 时仍须钉住；
6. muxed 兜底为空时，VR 专业表与单视频弹窗都产出 `bv*+ba` 组合选择器而非裸 best。

选择器用例以 SimpleNamespace 充当 self 调真实方法体——只碰目标分支读到的几个 Python 属性，
不实例化任何 Qt 控件，规避 Shiboken/主题监听 QThread 的构造开销与 CI 段错误风险。
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# config_manager / logger 在 import 期就落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-vr-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.auth.auth_service import auth_service  # noqa: E402
from fluentytdl.core.config_manager import config_manager  # noqa: E402
from fluentytdl.youtube.youtube_service import youtube_service  # noqa: E402
from fluentytdl.youtube.yt_dlp_cli import ydl_opts_to_cli_args  # noqa: E402


def _yt_pc(opts) -> str:
    """逗号拼接的 youtube.player_client（沿用 test_sabr_client_fallback 的取法）。"""
    ea = opts.get("extractor_args", {})
    yt = ea.get("youtube", {}) if isinstance(ea, dict) else {}
    pc = yt.get("player_client")
    if isinstance(pc, (list, tuple)):
        return ",".join(str(x) for x in pc)
    return pc if isinstance(pc, str) else ""


class _Combo:
    """最小 ComboBox 替身：只提供 currentIndex()。"""

    def __init__(self, idx: int) -> None:
        self._i = idx

    def currentIndex(self) -> int:
        return self._i


@pytest.fixture
def isolated_accounts(tmp_path, monkeypatch):
    """把账号存储与当前账号映射重指到 tmp_path，隔离真实凭据（同 sabr 契约测试）。"""
    monkeypatch.setattr(auth_service, "_webview2_accounts_path", tmp_path / "accounts.json")
    monkeypatch.setattr(auth_service, "_webview2_accounts", {})
    monkeypatch.setattr(auth_service, "_current_webview2_account_ids", {})
    monkeypatch.setattr(auth_service, "_session_sabr_only", False)
    return tmp_path


@pytest.fixture
def pot_disabled():
    """关 POT / 清静态 Token：build_ydl_options 不等待 POT、不钉 default,mweb。"""
    keys = ("pot_provider_enabled", "youtube_po_token")
    saved = {k: config_manager.get(k) for k in keys}
    config_manager.config["pot_provider_enabled"] = False
    config_manager.config["youtube_po_token"] = ""
    yield
    config_manager.config.update(saved)


# ── A. 覆盖层原语：覆盖 player_client、保留 POT、剥离 cookie ──────────


def test_overlay_overrides_player_client_and_preserves_pot():
    opts = {
        "cookiefile": "/tmp/run/cookies_youtube.txt",
        "skip_download": True,
        "extractor_args": {
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
            "youtube": {"player_client": ["default,mweb"], "fetch_pot": ["never"]},
        },
    }
    youtube_service.apply_android_vr_overlay(opts)

    # player_client 被覆盖为单一 android_vr（不是追加）
    assert _yt_pc(opts) == "android_vr"
    # POT Provider 独立 ie_key 原样保留（与 player_client 正交）
    assert opts["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://127.0.0.1:4416"]
    }
    # youtube 命名空间的兄弟键（POT 关闭时的 fetch_pot=never）不被整键 update 抹掉
    assert opts["extractor_args"]["youtube"]["fetch_pot"] == ["never"]
    # android_vr 不支持 cookies：cookiefile 被剥离
    assert "cookiefile" not in opts


def test_overlay_on_empty_opts_builds_nested_structure():
    opts: dict = {}
    youtube_service.apply_android_vr_overlay(opts)
    assert _yt_pc(opts) == "android_vr"
    assert "cookiefile" not in opts


# ── B. 转 CLI：argv 钉 android_vr、保留 POT 条目、无 --cookies ──────


def test_overlay_cli_argv_pins_android_vr_and_strips_cookies():
    # 模拟下载端 merged（build_ydl_options 产物）应用覆盖层后的样子
    merged = {
        "cookiefile": "/tmp/run/cookies_youtube.txt",
        "extractor_args": {
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
            "youtube": {"player_client": ["default,mweb"], "fetch_pot": ["never"]},
        },
    }
    youtube_service.apply_android_vr_overlay(merged)
    args = ydl_opts_to_cli_args(merged)

    ea_values = [args[i + 1] for i, a in enumerate(args) if a == "--extractor-args"]
    yt_arg = next(v for v in ea_values if v.startswith("youtube:"))
    assert "player_client=android_vr" in yt_arg
    assert "default,mweb" not in yt_arg
    # POT Provider 那条独立 --extractor-args 仍在
    assert any(v.startswith("youtubepot-bgutilhttp:") and "base_url=" in v for v in ea_values)
    # cookiefile 被剥离 → argv 不含 --cookies
    assert "--cookies" not in args


def test_workers_vr_flag_triggers_overlay_gate():
    """下载端只在 __fluentytdl_use_android_vr 为真时重钉（Group B 的门控语义）。"""
    merged = {
        "__fluentytdl_use_android_vr": True,
        "extractor_args": {"youtube": {"player_client": ["default"]}},
    }
    if merged.get("__fluentytdl_use_android_vr"):
        youtube_service.apply_android_vr_overlay(merged)
    assert _yt_pc(merged) == "android_vr"


def test_workers_no_flag_leaves_client_untouched():
    merged = {"extractor_args": {"youtube": {"player_client": ["default"]}}}
    if merged.get("__fluentytdl_use_android_vr"):
        youtube_service.apply_android_vr_overlay(merged)
    assert _yt_pc(merged) == "default"


# ── C. 解析用 opts：build_ydl_options + skip_download + 覆盖层 ──────


def test_build_vr_ydl_options_pins_android_vr(isolated_accounts, pot_disabled):
    vr_opts = youtube_service._build_vr_ydl_options(url="https://www.youtube.com/watch?v=dummyVRid")
    assert vr_opts.get("skip_download") is True
    assert _yt_pc(vr_opts) == "android_vr"
    # 即使真相源存在，覆盖层也已剥离 cookiefile（android_vr 无 cookie）
    assert "cookiefile" not in vr_opts


# ── D. 能力探测：看协议/能力，不看频道名 ──────────────────────────


def test_probe_vr_capability_sabr_only():
    formats = [
        {
            "format_id": "sb-v1",
            "protocol": "sabr",
            "vcodec": "vp9",
            "acodec": "none",
            "height": 2160,
        },
        {
            "format_id": "sb-v2",
            "protocol": "sabr",
            "vcodec": "av01",
            "acodec": "none",
            "height": 4320,
        },
        {"format_id": "sb-a1", "protocol": "sabr", "vcodec": "none", "acodec": "opus"},
    ]
    cap = youtube_service._probe_vr_capability(formats)
    assert cap["has_sabr"] is True
    assert cap["has_muxed"] is False  # SABR 核心无 combined → 单文件兜底消失
    assert cap["has_https_direct"] is False
    assert cap["protocols"] == {"sabr": 3}
    assert cap["total"] == 3


def test_probe_vr_capability_direct_and_muxed_mixed():
    formats = [
        # itag18 式 muxed https 直链（官方核心兜底）
        {"format_id": "18", "protocol": "https", "vcodec": "avc1", "acodec": "mp4a", "height": 360},
        {
            "format_id": "sb-v",
            "protocol": "sabr",
            "vcodec": "vp9",
            "acodec": "none",
            "height": 2160,
        },
        {"format_id": "251", "protocol": "https", "vcodec": "none", "acodec": "opus"},
        "not-a-dict",  # 非 dict 应被跳过
    ]
    cap = youtube_service._probe_vr_capability(formats)
    assert cap["has_muxed"] is True
    assert cap["has_https_direct"] is True
    assert cap["has_sabr"] is True
    assert cap["protocols"] == {"https": 2, "sabr": 1}
    assert cap["total"] == 3


# ── D2. 下载端客户端决策：两个核心各自判断、各走各路 ───────────────


def test_merge_formats_writes_vr_capability():
    """合并路径也必须写能力画像 —— 否则「解析出 VR 格式却没画像」的任务到了下载端
    只能走保守分支，SABR 核心下照样卡死。画像只描述 android_vr 那一侧的产物。"""
    info = {
        "formats": [
            {"format_id": "137", "protocol": "https", "vcodec": "avc1", "acodec": "none"},
            {"format_id": "140", "protocol": "https", "vcodec": "none", "acodec": "mp4a"},
        ]
    }
    vr_formats = [
        {
            "format_id": "sb-v",
            "protocol": "sabr",
            "vcodec": "vp9",
            "acodec": "none",
            "height": 4320,
        },
        {"format_id": "sb-a", "protocol": "sabr", "vcodec": "none", "acodec": "opus"},
    ]
    merged = youtube_service._merge_formats(info, vr_formats)

    cap = merged["__vr_capability"]
    assert cap["has_sabr"] is True
    # 主解析的 https 格式不得混进画像（否则误判成「有直链可下」）
    assert cap["has_https_direct"] is False
    assert cap["total"] == 2
    assert merged["__vr_only_format_ids"] == ["sb-v", "sb-a"]
    # 画像立刻可用于下载端决策
    pin, reason = youtube_service.plan_vr_download_client(
        {"format": "sb-v+sb-a", "__vr_capability": cap, "__vr_only_format_ids": ["sb-v", "sb-a"]}
    )
    assert (pin, reason) == (False, "sabr_not_deliverable")


def test_merge_formats_no_new_formats_writes_no_capability():
    """一个新格式都没加进来时不写画像（避免凭空多出一条「决策依据」）。"""
    info = {
        "formats": [{"format_id": "18", "protocol": "https", "vcodec": "avc1", "acodec": "mp4a"}]
    }
    merged = youtube_service._merge_formats(info, [dict(info["formats"][0])])
    assert "__vr_capability" not in merged


def test_download_deliverable_true_on_official_core_capability():
    """官方核心：android_vr 回 https 直链、无 sabr → 协议上可交付。

    注意这只是**必要条件**：直链仍受 GVS POT 策略约束（实测钉住后 403），所以
    plan_vr_download_client 还要再过一道必要性判据。
    """
    cap = {
        "total": 1,
        "protocols": {"https": 1},
        "has_muxed": True,
        "has_https_direct": True,
        "has_sabr": False,
    }
    assert youtube_service.android_vr_download_deliverable(cap) is True


def test_download_deliverable_false_when_core_turns_android_vr_into_sabr():
    """SABR 核心：has_sabr 即协议级信号 —— 签不出 android_vr 的 GVS POT，钉了必卡死。"""
    cap = {
        "total": 11,
        "protocols": {"sabr": 10, "https": 1},
        "has_muxed": False,
        "has_https_direct": True,
        "has_sabr": True,
    }
    assert youtube_service.android_vr_download_deliverable(cap) is False


@pytest.mark.parametrize("cap", [None, "", {}, [], 0])
def test_download_deliverable_missing_capability_keeps_legacy_behaviour(cap):
    """能力画像缺失（历史任务 / DB 恢复的旧 opts）→ 保守维持既有行为。"""
    assert youtube_service.android_vr_download_deliverable(cap) is True


def test_plan_keeps_default_clients_when_no_capability():
    """画像缺失且格式非独占 → 不钉。缺信息时默认客户端才是安全边。"""
    pin, reason = youtube_service.plan_vr_download_client({"format": "299+140"})
    assert pin is False
    assert reason == "format_shared_with_default_clients"


def test_plan_keeps_default_clients_for_shared_format_on_official_core():
    """回归护栏：官方核心（has_sabr=False）+ 默认客户端也看得见的格式 → 不许钉。

    android_vr 是匿名客户端、直链受 GVS POT 策略约束而 bgutil 签不出它的 token，
    实测官方核心 2026.08.19 与 SABR 核心 2026.09.28 钉住后都是 HTTP 403。默认客户端
    带 cookie 且拿得到 POT，能看见同一批 itag 时钉 android_vr 纯属增加失败面。
    """
    pin, reason = youtube_service.plan_vr_download_client(
        {"format": "18", "__vr_capability": {"has_sabr": False}}
    )
    assert pin is False
    assert reason == "format_shared_with_default_clients"


def test_plan_keeps_default_clients_under_sabr_core():
    pin, reason = youtube_service.plan_vr_download_client(
        {
            "format": "sb-v2+sb-a1",
            "__vr_capability": {"has_sabr": True},
            "__vr_only_format_ids": ["sb-v2", "sb-a1"],
        }
    )
    assert pin is False
    assert reason == "sabr_not_deliverable"


def test_plan_pins_for_android_vr_exclusive_format_on_deliverable_core():
    """非钉不可：格式是 android_vr 独占（默认客户端解析不出来）且协议上可交付。

    这是唯一还钉的场景 —— 配了 android_vr 静态 GVS po_token 的环境下它就是通的，
    拿不到 token 则由失败诊断说话，比静默换成另一条流诚实。
    """
    pin, reason = youtube_service.plan_vr_download_client(
        {
            "format": "vr-only-1+140",
            "__vr_capability": {"has_sabr": False},
            "__vr_only_format_ids": ["vr-only-1"],
        }
    )
    assert pin is True
    assert reason == "vr_only_format"


def test_plan_ignores_empty_vr_only_ids():
    pin, reason = youtube_service.plan_vr_download_client(
        {
            "format": "299+140",
            "__vr_capability": {"has_sabr": True},
            "__vr_only_format_ids": ["", None],
        }
    )
    assert pin is False
    assert reason == "format_shared_with_default_clients"


def test_unpinned_branch_appends_bv_ba_degrade_selector():
    """不钉时给选择器挂一层组合兜底：某个 ID 只在 android_vr 侧存在时降级而非硬报错。

    复刻 workers.py Group B 的 else 分支表达式（该分支内联在 DownloadWorker.run 里，
    没法脱离 Qt/staging 单独调用）。
    """
    merged = {"format": "299+140", "__vr_capability": {"has_sabr": True}}
    pin, _ = youtube_service.plan_vr_download_client(merged)
    assert pin is False

    fmt = merged.get("format")
    if isinstance(fmt, str) and fmt.strip() and "bv*+ba/b" not in fmt:
        merged["format"] = f"{fmt}/bv*+ba/b"
    assert merged["format"] == "299+140/bv*+ba/b"

    # 幂等：重复套用不会叠出第二段兜底
    fmt = merged.get("format")
    if isinstance(fmt, str) and fmt.strip() and "bv*+ba/b" not in fmt:
        merged["format"] = f"{fmt}/bv*+ba/b"
    assert merged["format"].count("bv*+ba/b") == 1


def test_unpinned_vr_task_keeps_vr_postprocess_flag():
    """不钉客户端 ≠ 不是 VR 任务：EAC→Equi 只看 __fluentytdl_use_android_vr。"""
    merged = {
        "__fluentytdl_use_android_vr": True,
        "format": "299+140",
        "__vr_capability": {"has_sabr": True},
        "extractor_args": {"youtube": {"player_client": ["default", "web_safari"]}},
    }
    pin, _ = youtube_service.plan_vr_download_client(merged)
    if pin:
        youtube_service.apply_android_vr_overlay(merged)

    assert merged["__fluentytdl_use_android_vr"] is True
    # 客户端集保持 build_ydl_options 的产物（含 SABR-only 账号的 web_safari 兜底）
    assert _yt_pc(merged) == "default,web_safari"


# ── E. 选择层：muxed 兜底为空 → bv*+ba 组合而非裸 best ─────────────
def test_vr_pro_table_empty_muxed_falls_back_to_bv_ba():
    from fluentytdl.ui.components.platforms.vr import VRFormatTableWidget

    fake = SimpleNamespace(mode_combo=_Combo(1), _selected_muxed_id=None)
    res = VRFormatTableWidget.get_selection_result(fake)
    assert res["format"] == "bv*+ba/b"


def test_vr_pro_table_muxed_selected_returns_id():
    from fluentytdl.ui.components.platforms.vr import VRFormatTableWidget

    fake = SimpleNamespace(mode_combo=_Combo(1), _selected_muxed_id="18")
    res = VRFormatTableWidget.get_selection_result(fake)
    assert res["format"] == "18"


def test_selection_dialog_single_muxed_empty_falls_back():
    from fluentytdl.ui.components.dialogs.selection_dialog import SelectionDialog

    fake = SimpleNamespace(
        _single_mode_combo=_Combo(1), _single_selected_muxed_id=None, video_info=None
    )
    opts = SelectionDialog.get_download_options(fake)
    assert opts["format"] == "best[acodec!=none][vcodec!=none]/bv*+ba/b"


def test_selection_dialog_single_muxed_selected_returns_id():
    from fluentytdl.ui.components.dialogs.selection_dialog import SelectionDialog

    fake = SimpleNamespace(
        _single_mode_combo=_Combo(1), _single_selected_muxed_id="22", video_info=None
    )
    opts = SelectionDialog.get_download_options(fake)
    assert opts["format"] == "22"
