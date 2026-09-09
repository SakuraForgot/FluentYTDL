"""两个真相源（bin/cookies_youtube.txt / bin/cookies_twitter.txt）的写入闸门。

用户设计的状态机是"刷新用户 cookie → 获取 → 复制替换真相源，并附带弱回退：
不可用不强制替换"。改造前四个写入点全是无条件 `shutil.copy2`，"提取成功但内容是
半份/已过期"照写不误 —— 弱回退机制根本不存在。这些测试是那条语义的护栏。

`CookieSentinel` 是单例，且开发环境下 `_base_dir` 指向仓库真实的 `bin/`。
所有测试必须先把 `_base_dir` 挪到 tmp_path，否则会覆盖开发者本机的真实 Cookie。
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-cookie-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.auth.auth_service import (  # noqa: E402
    BROWSER_COMBO_ITEMS,
    BROWSER_COMBO_LABELS,
    BROWSER_SOURCES,
    LEGACY_SOURCES,
    AuthSourceType,
    auth_service,
    browser_combo_index,
    browser_source_at,
)
from fluentytdl.auth.cookie_cleaner import CookieCleaner  # noqa: E402
from fluentytdl.auth.cookie_sentinel import cookie_sentinel  # noqa: E402

FUTURE = int(time.time()) + 86400 * 30
PAST = int(time.time()) - 86400


def _netscape(rows: list[tuple[str, int, str, str]]) -> str:
    """(domain, expires, name, value) → Netscape 七字段文本"""
    lines = ["# Netscape HTTP Cookie File", ""]
    for domain, expires, name, value in rows:
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        lines.append(f"{domain}\t{flag}\t/\tTRUE\t{expires}\t{name}\t{value}")
    return "\n".join(lines) + "\n"


def _youtube_full(expires: int = FUTURE) -> str:
    return _netscape(
        [(".youtube.com", expires, name, f"v-{name}") for name in
         ("SID", "HSID", "SSID", "SAPISID", "APISID")]
        + [(".youtube.com", expires, "LOGIN_INFO", "v-LOGIN_INFO")]
    )


def _youtube_partial() -> str:
    """只有 LOGIN_INFO —— 缺少全部必需字段"""
    return _netscape([(".youtube.com", FUTURE, "LOGIN_INFO", "v-LOGIN_INFO")])


def _youtube_google_only(expires: int = FUTURE) -> str:
    """yt-dlp 回写登出态后的典型残骸：SID 家族只剩在 `.google.com` 上（那一侧能挺过回写），
    `.youtube.com` 上一个登录态 marker 都没有（LOGIN_INFO 被剥掉），只剩访客态 Cookie。
    name-only 必需字段检查会被它骗过（SID/HSID/... 名字都在），域感知闸门必须挡下。"""
    return _netscape(
        [(".google.com", expires, name, f"v-{name}") for name in
         ("SID", "HSID", "SSID", "SAPISID", "APISID")]
        + [(".youtube.com", expires, "VISITOR_INFO1_LIVE", "v-guest"),
           (".youtube.com", expires, "PREF", "v-pref")]
    )


def _youtube_login_realistic(expires: int = FUTURE) -> str:
    """更贴近真实登录 jar 的形态：SID 家族在 `.google.com`，LOGIN_INFO 在 `.youtube.com`。
    必需字段齐全，且 `.youtube.com` 上有登录态 marker —— 闸门应放行。"""
    return _netscape(
        [(".google.com", expires, name, f"v-{name}") for name in
         ("SID", "HSID", "SSID", "SAPISID", "APISID")]
        + [(".youtube.com", expires, "LOGIN_INFO", "v-LOGIN_INFO"),
           (".youtube.com", expires, "__Secure-1PSID", "v-1psid")]
    )


def _twitter_full(expires: int = FUTURE) -> str:
    return _netscape(
        [(".x.com", expires, "auth_token", "v-auth"), (".x.com", expires, "ct0", "v-ct0")]
    )


@pytest.fixture
def sentinel(tmp_path, monkeypatch):
    """把单例的真相源目录挪进 tmp_path，避免污染仓库 bin/"""
    monkeypatch.setattr(cookie_sentinel, "_base_dir", tmp_path)
    monkeypatch.setattr(cookie_sentinel, "cookie_path", tmp_path / "cookies_youtube.txt")
    monkeypatch.setattr(cookie_sentinel, "meta_path", tmp_path / "cookies_youtube.txt.meta")
    monkeypatch.setattr(cookie_sentinel, "_commit_warnings", {})
    monkeypatch.setattr(cookie_sentinel, "_updating", set())
    return cookie_sentinel


# ==================== 弱回退 ====================


def test_weak_fallback_keeps_old_file_byte_identical(sentinel, tmp_path):
    """新 Cookie 缺必需字段 → 返回 False，旧真相源字节级不变"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_full(), encoding="utf-8")
    before = dest.read_bytes()

    src = tmp_path / "new.txt"
    src.write_text(_youtube_partial(), encoding="utf-8")

    ok, reason = sentinel._commit_to_truth_source(src, "youtube", "edge")

    assert ok is False
    assert "SID" in reason  # 缺失字段被点名
    assert dest.read_bytes() == before
    assert sentinel.get_commit_warning("youtube") == reason


def test_weak_fallback_rejects_expired_cookies(sentinel, tmp_path):
    """必需字段都在但全部已过期 → 同样不许覆盖"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_full(), encoding="utf-8")
    before = dest.read_bytes()

    src = tmp_path / "expired.txt"
    src.write_text(_youtube_full(expires=PAST), encoding="utf-8")

    ok, _reason = sentinel._commit_to_truth_source(src, "youtube", "edge")

    assert ok is False
    assert dest.read_bytes() == before


def test_commit_succeeds_and_writes_meta(sentinel, tmp_path):
    """必需字段齐全 → 覆盖成功，元数据记录来源，失败标记被清除"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_partial(), encoding="utf-8")
    sentinel._commit_warnings["youtube"] = "上一轮失败"

    src = tmp_path / "good.txt"
    src.write_text(_youtube_full(), encoding="utf-8")

    ok, _msg = sentinel._commit_to_truth_source(src, "youtube", "webview2:acc-1")

    assert ok is True
    assert dest.read_bytes() == src.read_bytes()
    assert sentinel.get_commit_warning("youtube") is None

    meta = json.loads(sentinel.get_meta_path_for_platform("youtube").read_text(encoding="utf-8"))
    assert meta["source"] == "webview2:acc-1"
    assert meta["cookie_count"] == 6


def test_non_netscape_source_is_rejected(sentinel, tmp_path):
    """JSON 格式的 Cookie 文件解析不出七字段 → 拒绝，不产生真相源"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    src = tmp_path / "cookies.json"
    src.write_text('[{"name": "SID", "value": "x"}]', encoding="utf-8")

    ok, _reason = sentinel._commit_to_truth_source(src, "youtube", "file")

    assert ok is False
    assert not dest.exists()


# ==================== 提交闸门：域感知的登录态 marker ====================


def test_commit_gate_rejects_google_only_residue_over_good_truth(sentinel, tmp_path):
    """只剩 `.google.com` SID 家族、`.youtube.com` 无登录态 marker 的半 jar
    （yt-dlp 回写登出态后的残骸）不得覆盖一个可用真相源。

    这正是「日志里 Valid=True、yt-dlp 却判未登录」的根因：旧的 name-only 检查忽略域，
    SID 家族在 `.google.com` 上齐全就放行，于是一份被回写成登出态的残骸能把好文件覆盖掉。
    域感知闸门补上这一刀。"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_login_realistic(), encoding="utf-8")
    before = dest.read_bytes()

    src = tmp_path / "google_only_residue.txt"
    src.write_text(_youtube_google_only(), encoding="utf-8")

    ok, reason = sentinel._commit_to_truth_source(src, "youtube", "edge")

    assert ok is False
    assert ".youtube.com" in reason  # 原因点名缺 .youtube.com 登录态
    assert dest.read_bytes() == before  # 真相源纹丝不动


def test_commit_gate_accepts_jar_with_youtube_login_marker(sentinel, tmp_path):
    """SID 家族在 `.google.com`、LOGIN_INFO 在 `.youtube.com` 的真实登录形态 —— 放行。

    确保闸门不误伤真实 jar：markers 只要有一个真的落在 `.youtube.com` 上即可。"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_partial(), encoding="utf-8")

    src = tmp_path / "good_realistic.txt"
    src.write_text(_youtube_login_realistic(), encoding="utf-8")

    ok, _msg = sentinel._commit_to_truth_source(src, "youtube", "webview2:acc-1")

    assert ok is True
    assert dest.read_bytes() == src.read_bytes()


# ==================== 原子性 ====================


def test_replace_failure_leaves_no_tmp_and_keeps_old(sentinel, tmp_path, monkeypatch):
    """os.replace 抛异常 → 目的地仍是旧内容，且没有残留 .tmp"""
    dest = sentinel.get_cookie_path_for_platform("youtube")
    dest.write_text(_youtube_full(), encoding="utf-8")
    before = dest.read_bytes()

    src = tmp_path / "new.txt"
    src.write_text(_youtube_full(expires=FUTURE + 999), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)

    ok, reason = sentinel._commit_to_truth_source(src, "youtube", "edge")

    assert ok is False
    assert "写入真相源失败" in reason
    assert dest.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []


# ==================== 平台隔离 ====================


def test_youtube_file_cannot_overwrite_twitter_truth_source(sentinel, tmp_path):
    """只含 YouTube 字段的文件不得写进 cookies_twitter.txt"""
    src = tmp_path / "yt.txt"
    src.write_text(_youtube_full(), encoding="utf-8")

    ok, _reason = sentinel._commit_to_truth_source(src, "twitter", "file")

    assert ok is False
    assert not sentinel.get_cookie_path_for_platform("twitter").exists()


def test_twitter_commit_leaves_youtube_untouched(sentinel, tmp_path):
    """写 twitter 真相源不碰 youtube 真相源"""
    yt_dest = sentinel.get_cookie_path_for_platform("youtube")
    yt_dest.write_text(_youtube_full(), encoding="utf-8")
    yt_before = yt_dest.read_bytes()

    src = tmp_path / "x.txt"
    src.write_text(_twitter_full(), encoding="utf-8")

    ok, _msg = sentinel._commit_to_truth_source(src, "twitter", "webview2:x-1")

    assert ok is True
    assert sentinel.get_cookie_path_for_platform("twitter").exists()
    assert yt_dest.read_bytes() == yt_before


# ==================== 合规清洗不吃掉 X ====================


def test_cleaner_preserves_x_cookies_with_correct_platform():
    """twitter 分支不做 name 过滤 —— auth_token / ct0 必须活着"""
    x_cookies = [
        {"domain": ".x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "auth_token", "value": "a"},
        {"domain": ".x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "ct0", "value": "c"},
    ]

    kept = {c["name"] for c in CookieCleaner.clean(x_cookies, "twitter", True)}
    assert {"auth_token", "ct0"} <= kept


def test_cleaner_washes_x_cookies_when_platform_is_wrong():
    """传错 platform 就会被 YOUTUBE_ALLOWED_NAMES 剥光 —— 串台调用的回归护栏"""
    x_cookies = [
        {"domain": ".x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "auth_token", "value": "a"},
        {"domain": ".x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "ct0", "value": "c"},
    ]

    assert CookieCleaner.clean(x_cookies, "youtube", True) == []


def test_cleaner_rejects_bare_tld_domain():
    """`.com` 不再能蒙过 `.x.com` 白名单。

    旧实现的反向匹配 `d.endswith(domain)` 让 `".x.com".endswith(".com")` 成立，
    于是任何顶级域 Cookie 都能混进 X 的真相源。只保留"相等或子域"。
    """
    cookies = [
        {"domain": ".com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "auth_token", "value": "a"},
        {"domain": "notx.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "ct0", "value": "c"},
    ]

    assert CookieCleaner.clean(cookies, "twitter", True) == []

    # 真正的子域照常放行，前缀点在两边都不影响判定
    subdomain = [
        {"domain": "mobile.twitter.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "auth_token", "value": "a"},
        {"domain": "x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "ct0", "value": "c"},
    ]
    assert len(CookieCleaner.clean(subdomain, "twitter", True)) == 2


def test_cleaner_expiry_drop_is_independent_of_cleaning_switch():
    """丢弃过期是独立维度：`enable_cleaning=False` 不再顺手替用户丢掉过期条目"""
    rows = [
        {"domain": ".x.com", "path": "/", "secure": True, "expires": PAST,
         "name": "auth_token", "value": "a"},
        {"domain": ".x.com", "path": "/", "secure": True, "expires": 0,
         "name": "ct0", "value": "c"},  # 会话 Cookie，expires==0 不算过期
    ]

    # 默认行为不变：过期的被丢，会话 Cookie 留下
    kept = CookieCleaner.clean(rows, "twitter", True)
    assert [c["name"] for c in kept] == ["ct0"]

    # 显式关掉后过期条目原样保留 —— 这才是"关闭清理"该有的语义
    raw = CookieCleaner.clean(rows, "twitter", False, drop_expired=False)
    assert [c["name"] for c in raw] == ["auth_token", "ct0"]


def test_cleaner_drops_the_bogus_flag_key():
    """输出只允许 Netscape 七字段的子集，`flag` 是写文件时才推算的，不该留在数据里"""
    cookies = [
        {"domain": ".x.com", "path": "/", "secure": True, "expires": FUTURE,
         "name": "auth_token", "value": "a", "httpOnly": True, "sameSite": "None"},
    ]

    out = CookieCleaner.clean(cookies, "twitter", True)

    assert set(out[0]) <= CookieCleaner.NETSCAPE_FIELDS
    assert "flag" not in out[0]


# ==================== 按平台互斥锁 ====================


def test_refresh_lock_rejects_same_platform_only(sentinel, monkeypatch):
    """youtube 刷新期间：youtube 被拒，twitter 放行（不再返回"正在更新中"）"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.NONE)
    sentinel._updating.add("youtube")

    ok_same, msg_same = sentinel.force_refresh_with_uac(platform="youtube")
    assert ok_same is False
    assert "正在更新中" in msg_same

    # twitter 越过 busy 检查后，因为验证源为 NONE 而在下一步退出 —— 证明没被锁挡住
    ok_other, msg_other = sentinel.force_refresh_with_uac(platform="twitter")
    assert ok_other is False
    assert "正在更新中" not in msg_other
    assert "未配置验证源" in msg_other

    # finally 只释放自己占用的平台
    assert sentinel._updating == {"youtube"}


# ==================== 启动健康度 ====================


def test_startup_health_skips_platform_without_truth_source(sentinel, monkeypatch):
    """只有 cookies_twitter.txt 时 youtube.enabled 为 False —— 只用 X 的用户不该被误伤"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.EDGE)
    sentinel.get_cookie_path_for_platform("twitter").write_text(_twitter_full(), encoding="utf-8")

    health = sentinel.get_startup_health()

    assert health["youtube"]["enabled"] is False
    assert health["youtube"]["exists"] is False
    assert health["twitter"]["enabled"] is True
    assert health["twitter"]["valid"] is True


def test_startup_health_webview2_uses_account_list(sentinel, monkeypatch):
    """WebView2 模式下账号列表才是用户意图：没有 youtube 账号，就算文件在也不提醒"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.WEBVIEW2)
    sentinel.get_cookie_path_for_platform("youtube").write_text(_youtube_full(), encoding="utf-8")
    monkeypatch.setattr(
        auth_service,
        "list_webview2_accounts",
        lambda platform=None: ["acc-x"] if platform == "twitter" else [],
    )

    health = sentinel.get_startup_health()

    assert health["youtube"]["enabled"] is False  # 文件存在但用户没登录过 YouTube
    assert health["youtube"]["valid"] is True  # 文件本身照常体检
    assert health["twitter"]["enabled"] is True


def test_startup_health_reports_invalid_file(sentinel, monkeypatch):
    """半份 Cookie → enabled 但 valid=False，这才是该弹 warning 的那一档"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.EDGE)
    sentinel.get_cookie_path_for_platform("youtube").write_text(
        _youtube_partial(), encoding="utf-8"
    )

    yt = sentinel.get_startup_health()["youtube"]

    assert yt["enabled"] is True
    assert yt["exists"] is True
    assert yt["valid"] is False
    assert yt["reason"]  # 缺失原因必须能说出口，UI 要直接展示它


def test_startup_health_surfaces_commit_warning(sentinel, monkeypatch):
    """闸门拒绝过新 Cookie → 健康度带上原因，UI 才能说"仍在使用旧文件\""""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.EDGE)
    sentinel.get_cookie_path_for_platform("youtube").write_text(_youtube_full(), encoding="utf-8")
    sentinel._commit_warnings["youtube"] = "缺少必需字段: SID"

    yt = sentinel.get_startup_health()["youtube"]

    assert yt["valid"] is True  # 旧文件还好着
    assert yt["commit_warning"] == "缺少必需字段: SID"


def test_emit_startup_health_delivers_dict_once(sentinel, monkeypatch):
    """UI 的启动提醒完全靠这个信号：必须恰好发一次，且载荷是两平台的 dict"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.EDGE)
    received = []
    sentinel.startupHealthReady.connect(received.append)
    try:
        sentinel._emit_startup_health()
    finally:
        sentinel.startupHealthReady.disconnect(received.append)

    assert len(received) == 1
    assert set(received[0]) == {"youtube", "twitter"}
    assert "enabled" in received[0]["youtube"]


# ==================== 设置页状态卡 ====================


def test_status_info_carries_commit_warning(sentinel, monkeypatch):
    """设置页状态卡靠 get_status_info 拿闸门原因 —— 少了这个键，刷新失败就只剩日志"""
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.EDGE)
    sentinel.get_cookie_path_for_platform("youtube").write_text(_youtube_full(), encoding="utf-8")
    sentinel._commit_warnings["youtube"] = "缺少必需字段: SID"

    info = sentinel.get_status_info("youtube")

    assert info["cookie_valid"] is True  # 旧文件还能用
    assert info["commit_warning"] == "缺少必需字段: SID"
    # 另一个平台没被拒过，不该串味
    assert sentinel.get_status_info("twitter")["commit_warning"] is None


# ==================== 已停止支持的提取源（Chrome / 百分浏览器） ====================


def test_legacy_source_config_migrates_to_edge(tmp_path, monkeypatch):
    """老配置里的 `"source": "chrome"` 必须在加载时迁到 Edge 并写回文件。

    枚举成员故意保留：删掉会让 `AuthSourceType("chrome")` 抛 ValueError，
    用户升级后启动即报错。迁移必须发生在服务层 —— 靠 UI 下拉框兜底是不行的，
    下拉框选不中 chrome 时只会停在索引 0，配置里仍是 chrome。
    """
    cfg = tmp_path / "auth_config.json"
    cfg.write_text(json.dumps({"version": 3, "source": "chrome"}), encoding="utf-8")

    monkeypatch.setattr(auth_service, "_config_path", cfg)
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.WEBVIEW2)
    # 只测迁移，不去碰状态恢复（会读缓存文件）
    monkeypatch.setattr(auth_service, "_restore_last_status", lambda: None)

    auth_service._load_config()

    assert auth_service.current_source is AuthSourceType.EDGE
    # 必须写回，否则下次启动又要迁一遍
    assert json.loads(cfg.read_text(encoding="utf-8"))["source"] == "edge"


def test_legacy_centbrowser_config_migrates_to_edge(tmp_path, monkeypatch):
    """百分浏览器同理 —— 它那份 110 行自建 DPAPI 提取器已随本次改动删除"""
    cfg = tmp_path / "auth_config.json"
    cfg.write_text(json.dumps({"version": 3, "source": "centbrowser"}), encoding="utf-8")

    monkeypatch.setattr(auth_service, "_config_path", cfg)
    monkeypatch.setattr(auth_service, "_current_source", AuthSourceType.WEBVIEW2)
    monkeypatch.setattr(auth_service, "_restore_last_status", lambda: None)

    auth_service._load_config()

    assert auth_service.current_source is AuthSourceType.EDGE
    assert json.loads(cfg.read_text(encoding="utf-8"))["source"] == "edge"


def test_browser_combo_excludes_legacy_sources():
    """下拉框里不能再出现 Chrome / 百分浏览器，否则用户选了必然提取失败"""
    listed = {source for source, _ in BROWSER_COMBO_ITEMS}

    assert not (listed & LEGACY_SOURCES)
    # BROWSER_SOURCES 由下拉框列表派生："选得到"与"代码认它是浏览器源"必须是同一集合
    assert BROWSER_SOURCES == [source for source, _ in BROWSER_COMBO_ITEMS]
    assert len(BROWSER_COMBO_LABELS) == len(BROWSER_COMBO_ITEMS)


def test_browser_combo_index_roundtrip():
    """索引 ↔ 提取源必须互为反函数。

    这条护栏针对的是被删掉的那五份手写位置列表：它们的顺序曾经彼此不一致
    （下载对话框把 Firefox 排在第 3 位，设置页排在第 9 位），错位后用户选
    "Firefox" 而程序去提取 Arc，且不报任何错。
    """
    for expected_index, (source, _) in enumerate(BROWSER_COMBO_ITEMS):
        assert browser_combo_index(source) == expected_index
        assert browser_source_at(expected_index) is source

    # 越界与已停止支持的源一律回落 Edge / 索引 0，不抛异常
    assert browser_source_at(-1) is AuthSourceType.EDGE
    assert browser_source_at(len(BROWSER_COMBO_ITEMS)) is AuthSourceType.EDGE
    assert browser_combo_index(AuthSourceType.CHROME) == 0
    assert browser_combo_index(AuthSourceType.CENT) == 0
