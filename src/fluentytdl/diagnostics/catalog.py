"""错误码 → 用户文案的映射。

文案与匹配逻辑彻底分离：引擎只产出稳定的字符串 code，展示文案在这里查表。
所有字符串用 ``QT_TRANSLATE_NOOP`` 标记以便 ``pyside6-lupdate`` 提取，实际翻译
在 ``describe()`` 调用时才发生 —— 运行时切换语言后重新读取即可拿到新语言。

新增错误码后请同步在这里补条目，``tests/test_error_rules_integrity.py`` 会断言
规则表里的每个 code 都有对应文案。
"""

from __future__ import annotations

from .models import FALLBACK_CODE


def QT_TRANSLATE_NOOP(_context: str, text: str) -> str:
    """标记待提取的翻译源串，原样返回。

    这里刻意自己实现而不从 PySide6 导入：Qt 侧的同名函数本就是恒等映射，而
    ``pyside6-lupdate`` 是按调用点的函数名扫描提取的，与实现来源无关。自己定义
    可以让本模块在无 Qt 环境（纯逻辑单测）下照样导入。
    """
    return text


try:
    from PySide6.QtCore import QCoreApplication

    def _translate(text: str) -> str:
        return QCoreApplication.translate("Diagnostics", text)

except ImportError:  # pragma: no cover - 无 Qt 环境（纯逻辑单测）下退化为原文

    def _translate(text: str) -> str:
        return text


#: fix_action → 按钮上的引导词。同一个动作在几十个码之间复用，不必逐码重复。
_FIX_HINTS: dict[str, str] = {
    "extract_cookie": QT_TRANSLATE_NOOP("Diagnostics", "导入 Cookie"),
    "relogin": QT_TRANSLATE_NOOP("Diagnostics", "重新登录"),
    "switch_proxy": QT_TRANSLATE_NOOP("Diagnostics", "检查代理设置"),
    "change_download_dir": QT_TRANSLATE_NOOP("Diagnostics", "更换下载路径"),
    "update_component": QT_TRANSLATE_NOOP("Diagnostics", "去更新组件"),
    "refresh_pot": QT_TRANSLATE_NOOP("Diagnostics", "重启 POT 服务"),
    "retry_now": QT_TRANSLATE_NOOP("Diagnostics", "立即重试"),
    "open_download_dir": QT_TRANSLATE_NOOP("Diagnostics", "打开下载目录"),
}


def hint_for(fix_action: str | None) -> str:
    """修复动作按钮上的提示词。无动作时返回空串。"""
    if not fix_action:
        return ""
    text = _FIX_HINTS.get(fix_action)
    return _translate(text) if text else _translate(QT_TRANSLATE_NOOP("Diagnostics", "去处理"))


def describe(code: str) -> tuple[str, str]:
    """错误码 → ``(标题, 正文)``，已本地化。未登记的码回退到 ``unknown``。"""
    entry = _ENTRIES.get(code) or _ENTRIES[FALLBACK_CODE]
    return _translate(entry[0]), _translate(entry[1])


def known_codes() -> frozenset[str]:
    return frozenset(_ENTRIES)


_ENTRIES: dict[str, tuple[str, str]] = {
    FALLBACK_CODE: (
        QT_TRANSLATE_NOOP("Diagnostics", "解析或下载失败"),
        QT_TRANSLATE_NOOP("Diagnostics", "系统遇到无法完全识别的错误，请查看错误原始日志。"),
    ),
    # ---- 工具链 ----
    "nsig_extraction_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "nsig 提取失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "yt-dlp 未能解出播放地址的 nsig 参数，部分格式会缺失或下载到一半被拒。"
            "这几乎总是站点改版导致，更新核心组件即可解决。",
        ),
    ),
    "signature_extraction_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "签名解密失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "无法解密视频地址的签名参数，通常意味着 yt-dlp 已落后于站点当前的播放器版本。",
        ),
    ),
    "ytdlp_outdated": (
        QT_TRANSLATE_NOOP("Diagnostics", "核心组件版本过旧"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "当前 yt-dlp 版本已明显落后，建议先更新再排查其他问题。"
        ),
    ),
    "pot_provider_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "POT 服务不可用"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "PO Token 提供程序（bgutil provider）没有响应。缺少 PO Token 时 YouTube 会"
            "拒绝大部分格式的下载请求。",
        ),
    ),
    "pot_token_required": (
        QT_TRANSLATE_NOOP("Diagnostics", "需要 PO Token 验证"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "服务端要求提供 PO Token 才肯下发播放地址。请确认 POT 服务正在运行，"
            "并且 Cookie 关联的账号状态正常。",
        ),
    ),
    "extractor_broken": (
        QT_TRANSLATE_NOOP("Diagnostics", "提取器失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "解析该网页内容时失败。可能是目标网站改版，建议更新解析核心组件。"
        ),
    ),
    "ffmpeg_not_found": (
        QT_TRANSLATE_NOOP("Diagnostics", "缺少核心组件 (FFmpeg)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "视频合并或封面处理需要 FFmpeg，但系统未找到该工具。"
        ),
    ),
    "ffmpeg_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "FFmpeg 处理失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "FFmpeg 在合并或转码阶段出错。可能是源文件已损坏，或输出路径不可写。",
        ),
    ),
    "vr_eac_conversion_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "VR 投影转换失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "EAC → 等距柱状投影的转换未完成，或空间元数据写入失败。视频本身通常仍可播放，"
            "但在 VR 播放器里可能显示为普通平面画面。",
        ),
    ),
    # ---- 认证与权限 ----
    "bot_check_sign_in": (
        QT_TRANSLATE_NOOP("Diagnostics", "人机验证拦截 (Bot 检测)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "服务提供商认为当前请求来自自动化工具。通常是节点 IP 触发了风控，"
            "或者当前 Cookie 已被标记。换一个干净的节点并重新导入 Cookie 往往能恢复。",
        ),
    ),
    "cookie_expired": (
        QT_TRANSLATE_NOOP("Diagnostics", "Cookie 已失效"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "账号 Cookie 已过期或被服务端轮换。重新提取一份新的 Cookie 即可继续。",
        ),
    ),
    "cookie_file_invalid": (
        QT_TRANSLATE_NOOP("Diagnostics", "Cookie 文件格式错误"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "yt-dlp 只接受 Netscape 格式的 cookies.txt。当前文件疑似 JSON 或其他格式，"
            "请通过应用内的 WebView2 登录重新生成。",
        ),
    ),
    "members_only": (
        QT_TRANSLATE_NOOP("Diagnostics", "会员专属视频"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "这是频道的会员专享内容，请确保使用的 Cookie 关联的账号已购买该频道会员。",
        ),
    ),
    "age_restricted": (
        QT_TRANSLATE_NOOP("Diagnostics", "年龄限制 (需要登录验证)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该视频有年龄限制，必须使用已验证年龄的账号才能访问。"
        ),
    ),
    "private_video": (
        QT_TRANSLATE_NOOP("Diagnostics", "私人视频"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "该视频已被上传者设置为私有，只有获得授权的账号才能访问。若你确实有权限，"
            "请确认 Cookie 来自那个账号。",
        ),
    ),
    "login_required": (
        QT_TRANSLATE_NOOP("Diagnostics", "需要登录验证"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "网站要求登录以确认身份。可能是 Cookie 缺失或已失效，也可能是该内容本身需要权限。",
        ),
    ),
    # ---- 媒体状态 ----
    "copyright_claimed": (
        QT_TRANSLATE_NOOP("Diagnostics", "版权封锁"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该内容因版权投诉被下架或屏蔽，换节点或换账号都无法绕过。"
        ),
    ),
    "geo_blocked": (
        QT_TRANSLATE_NOOP("Diagnostics", "地区限制"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "由于版权或区域限制，当前网络节点所在地区无法访问该内容。"
        ),
    ),
    "video_removed": (
        QT_TRANSLATE_NOOP("Diagnostics", "视频已被删除"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该视频已被平台或上传者永久删除，也可能是账号已被封禁。"
        ),
    ),
    "video_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "视频不可用"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "平台返回「视频不可用」。可能已被删除、转为私有，或在当前地区/账号下不可见。",
        ),
    ),
    "livestream_not_started": (
        QT_TRANSLATE_NOOP("Diagnostics", "直播尚未开始"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该直播还没开始推流，请等到开播后再下载。"
        ),
    ),
    "livestream_ended": (
        QT_TRANSLATE_NOOP("Diagnostics", "直播已结束"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "直播已经结束，且平台尚未生成回放。等待回放上线后通常就能正常下载。",
        ),
    ),
    "not_premiered_yet": (
        QT_TRANSLATE_NOOP("Diagnostics", "首映未开始"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该视频处于首映等待状态，尚未正式开播。"
        ),
    ),
    "playlist_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "播放列表不可用"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该播放列表不存在、已被删除，或被设置为私有。"
        ),
    ),
    "channel_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "频道页不可用"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "无法读取该频道的内容页。频道可能已注销，或该标签页（视频/直播/短片）本身不存在。",
        ),
    ),
    "embed_only": (
        QT_TRANSLATE_NOOP("Diagnostics", "仅限站内播放"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "上传者限制了播放场景，该视频只能在原站点内观看，无法通过外部工具提取。",
        ),
    ),
    "url_unsupported": (
        QT_TRANSLATE_NOOP("Diagnostics", "链接无效或不支持"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "提供的链接格式不正确，或者当前组件不支持解析该网站。"
        ),
    ),
    "format_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "所选格式不可用"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "指定的画质、编码或音轨在这个视频里不存在。换一档画质通常就能下载。",
        ),
    ),
    "no_formats_found": (
        QT_TRANSLATE_NOOP("Diagnostics", "无可用流媒体格式"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "页面里没有找到任何可下载的音视频流。若链接本身能正常播放，多半是解析组件已过时。",
        ),
    ),
    "input_filter_skipped": (
        QT_TRANSLATE_NOOP("Diagnostics", "已按过滤条件跳过"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "该条目不符合设定的过滤条件（上传日期、文件大小、数量上限等），已被跳过。"
            "这不是错误。",
        ),
    ),
    "already_downloaded": (
        QT_TRANSLATE_NOOP("Diagnostics", "已下载过"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "该视频已存在于下载记录中，本次已跳过。这不是错误。"
        ),
    ),
    # ---- 网络 ----
    "rate_limited_429": (
        QT_TRANSLATE_NOOP("Diagnostics", "请求过于频繁 (429)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "服务端对当前 IP 触发了限流。稍等一会儿或更换节点即可恢复，"
            "程序会自动退避重试。",
        ),
    ),
    "http_403_forbidden": (
        QT_TRANSLATE_NOOP("Diagnostics", "访问被拒绝 (403)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "服务端拒绝了下载请求。常见原因是节点 IP 被风控、播放地址已过期，"
            "或核心组件版本过旧。",
        ),
    ),
    "http_404_not_found": (
        QT_TRANSLATE_NOOP("Diagnostics", "资源不存在 (404)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "目标地址已失效或资源已被移除。请确认链接是否仍然有效。"
        ),
    ),
    "http_server_5xx": (
        QT_TRANSLATE_NOOP("Diagnostics", "服务器故障 (5xx)"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "目标网站的服务器暂时出错，与本地设置无关。程序会自动重试，稍后通常自行恢复。",
        ),
    ),
    "ssl_eof": (
        QT_TRANSLATE_NOOP("Diagnostics", "SSL 连接被意外中断"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "TLS 握手过程中连接被对端切断。这类中断多由代理链路不稳定引起，会自动重试。",
        ),
    ),
    "ssl_cert_invalid": (
        QT_TRANSLATE_NOOP("Diagnostics", "SSL 证书校验失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "无法验证服务器证书。常见于中间人式代理或系统时间错误，也可能是本地根证书缺失。",
        ),
    ),
    "proxy_connect_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "无法连接代理"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "代理服务器拒绝连接或未在监听。请确认代理软件已启动、端口填写正确；"
            "若使用 TUN 模式，请不要同时填写代理地址。",
        ),
    ),
    "dns_resolution_failed": (
        QT_TRANSLATE_NOOP("Diagnostics", "域名解析失败"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "无法把域名解析成 IP。可能是本机 DNS 被污染或断网，也可能是代理未生效。",
        ),
    ),
    "connection_timeout": (
        QT_TRANSLATE_NOOP("Diagnostics", "网络连接超时"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "连接在建立或读取阶段超时。请检查网络与代理是否通畅，程序会自动重试几次。",
        ),
    ),
    "download_interrupted": (
        QT_TRANSLATE_NOOP("Diagnostics", "下载中断"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "数据流在传输途中被截断，实际收到的字节数少于预期。通常重试即可完成。",
        ),
    ),
    "fragment_retry": (
        QT_TRANSLATE_NOOP("Diagnostics", "分片下载异常"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "部分视频分片获取失败。少量分片重试成功不影响成品，大量失败则会导致文件残缺。",
        ),
    ),
    "socket_unavailable": (
        QT_TRANSLATE_NOOP("Diagnostics", "网络不可达"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "操作系统报告目标网络不可达，请检查本机网络连通性与代理路由。"
        ),
    ),
    "sponsorblock_unreachable": (
        QT_TRANSLATE_NOOP("Diagnostics", "SponsorBlock 服务不可达"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "无法连接 SponsorBlock API，赞助片段的标记或移除会被跳过。视频本身不受影响。",
        ),
    ),
    # ---- 文件系统 ----
    "disk_full": (
        QT_TRANSLATE_NOOP("Diagnostics", "磁盘空间不足"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "目标磁盘已写满，无法继续写入。请清理空间或把下载目录换到别的分区。",
        ),
    ),
    "permission_denied": (
        QT_TRANSLATE_NOOP("Diagnostics", "文件访问被拒绝"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "没有写入权限，或目标文件正被其他程序占用。换一个下载目录通常能解决。",
        ),
    ),
    "filename_too_long": (
        QT_TRANSLATE_NOOP("Diagnostics", "文件名过长"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "生成的文件路径超出系统长度上限。请缩短下载目录层级，或调整命名模板。",
        ),
    ),
    "file_missing": (
        QT_TRANSLATE_NOOP("Diagnostics", "找不到文件"),
        QT_TRANSLATE_NOOP(
            "Diagnostics",
            "预期的文件或可执行程序不存在。可能是中间文件被安全软件清理，"
            "或组件安装不完整。",
        ),
    ),
    "out_of_memory": (
        QT_TRANSLATE_NOOP("Diagnostics", "内存不足"),
        QT_TRANSLATE_NOOP(
            "Diagnostics", "系统无法为处理进程分配足够内存，请关闭部分程序后重试。"
        ),
    ),
}
