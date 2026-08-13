from __future__ import annotations

import re


class UrlValidator:
    """URL 验证工具类"""

    # 覆盖常见的 YouTube URL 格式
    # 视频：watch?v=, embed/, v/, shorts/, live/
    # 频道：@Handle, channel/UCxxx, c/Name, user/Name（含可选标签页后缀）
    # 播放列表：playlist?list=
    # 短链接：youtu.be/xxx
    YOUTUBE_REGEX = (
        r"^(https?://)?(www\.)?(m\.)?(youtube\.com|youtu\.be)/"
        r"("
        r"@[\w.-]+(/(videos|shorts))?"  # 频道 @Handle（含可选标签页）
        r"|channel/[\w-]+(/(videos|shorts))?"  # 频道 ID
        r"|c/[\w.-]+(/(videos|shorts))?"  # 旧自定义 URL
        r"|user/[\w.-]+(/(videos|shorts))?"  # 旧用户名
        r"|watch\?v=[\w-]+"  # 标准视频
        r"|embed/[\w-]+"  # 嵌入
        r"|v/[\w-]+"  # 旧格式
        r"|shorts/[\w-]+"  # Shorts
        r"|live/[\w-]+"  # 直播
        r"|playlist\?list=[\w-]+"  # 播放列表
        r"|[\w-]+"  # 短链接 / 其他
        r")"
        r"(\?[\w=&.-]*)?"  # 可选查询参数
        r"$"
    )

    # 频道 URL 专用正则
    _CHANNEL_REGEX = (
        r"^(https?://)?(www\.)?(m\.)?youtube\.com/"
        r"("
        r"@[\w.-]+(/(videos|shorts))?"
        r"|channel/[\w-]+(/(videos|shorts))?"
        r"|c/[\w.-]+(/(videos|shorts))?"
        r"|user/[\w.-]+(/(videos|shorts))?"
        r")"
        r"(\?[\w=&.-]*)?$"
    )

    @staticmethod
    def is_youtube_url(text: str) -> bool:
        if not text:
            return False
        return bool(re.match(UrlValidator.YOUTUBE_REGEX, text.strip()))

    @staticmethod
    def is_channel_url(text: str) -> bool:
        """判断是否为 YouTube 频道 URL"""
        if not text:
            return False
        return bool(re.match(UrlValidator._CHANNEL_REGEX, text.strip()))

    # YouTube 视频 ID 提取：覆盖 watch?v= / youtu.be / shorts / embed / v / live。
    # 末尾的负向预查保证只吃满 11 位的 ID，不会从更长的 token（播放列表 ID、
    # 24 位频道 ID）里切出一段假 ID。
    _VIDEO_ID_RE = re.compile(
        r"(?:youtu\.be/|/shorts/|/embed/|/live/|/v/|[?&]v=)([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])"
    )

    @staticmethod
    def extract_video_id(text: str) -> str:
        """从 YouTube URL 里取出 11 位视频 ID，取不到返回空串。

        纯字符串解析，不发网络请求，因此可以在 UI 线程上直接调用。
        """
        if not text:
            return ""
        m = UrlValidator._VIDEO_ID_RE.search(text.strip())
        return m.group(1) if m else ""

    @staticmethod
    def youtube_thumbnail_url(text: str) -> str:
        """由 URL 直接推出 YouTube 缩略图地址，取不到 ID 时返回空串。

        用途是解析尚未返回时先把缩略图铺上去。选 mqdefault（320×180）：
        - maxresdefault / hq720 在不少视频上直接 404；
        - hqdefault 是 480×360 的 4:3，16:9 视频会带上下黑边，塞进 160×90 会变形；
        - mqdefault 对所有视频都存在，且正好是 16:9，缩到 160×90 是整数倍。
        """
        vid = UrlValidator.extract_video_id(text)
        return f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg" if vid else ""

    # X (Twitter) 推文视频 URL 正则
    X_STATUS_REGEX = (
        r"^(https?://)?(www\.)?(mobile\.)?"
        r"(twitter\.com|x\.com)/"
        r"(\w+|i(/web)?)/(?:status|statuses)/(\d+)"
        r"(\?[\w=&.-]*)?"
        r"$"
    )

    # X 平台域名检测 (包含镜像站)
    X_DOMAIN_REGEX = (
        r"^(https?://)?(www\.)?(mobile\.)?"
        r"(twitter\.com|x\.com|vxtwitter\.com|fxtwitter\.com|fixvx\.com|t\.co)"
    )

    @staticmethod
    def is_x_url(text: str) -> bool:
        """判断是否为 X 平台相关 URL"""
        if not text:
            return False
        return bool(re.match(UrlValidator.X_DOMAIN_REGEX, text.strip()))

    @staticmethod
    def is_x_video_url(text: str) -> bool:
        """判断是否为 X 平台推文视频 URL（可直接下载）"""
        if not text:
            return False
        return bool(re.match(UrlValidator.X_STATUS_REGEX, text.strip()))
