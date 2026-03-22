import re


def translate_error(exc: Exception | str) -> str:
    """
    拦截并翻译 yt-dlp 的原始报错，提取关键信息并转化为友好的中文提示。
    """
    msg = str(exc)

    # 网络拦截 / 请求频率
    if "HTTP Error 429" in msg or "Too Many Requests" in msg:
        return "请求过于频繁 (HTTP 429)，请稍后再试或切换网络节点。"
    if "HTTP Error 403" in msg or "Forbidden" in msg:
        return "访问被拒绝 (HTTP 403)，可能是视频需要特定权限或节点被 YouTube 封锁。"

    # 面向用户的限制
    if (
        "Sign in to confirm you're not a bot" in msg
        or "poToken" in msg
        or "Sign in to confirm" in msg
    ):
        return "触发 YouTube 人机验证拦截，推荐第一步更新 yt-dlp 组件，或在主界面开启「附加 Cookies」以绕过检测。"
    if "Video unavailable" in msg:
        return "视频不可用，可能已被作者删除或设置为私享。"
    if "Age restricted" in msg:
        return "该视频有年龄限制，请确保您注入了已登录的浏览器 Cookies。"

    # 解析相关
    if "Unsupported URL" in msg:
        return "不支持的链接格式，无法识别此 URL。"
    if "Sign in to confirm your age" in msg:
        return "此视频需要登录确认年龄，请注入 Cookies。"
    if "Members only content" in msg:
        return "此视频为频道会员专属内容，请注入包含会员权限的 Cookies。"
    if "ffmpeg is not installed" in msg:
        return "未检测到 FFmpeg 依赖，导致音视频合并失败，请重新配置环境。"

    # 将一些带有回溯栈的长报错精简
    match = re.search(r"ERROR: \[(.*?)\] (.*)", msg)
    if match:
        extractor = match.group(1)
        reason = match.group(2)
        # 脱敏清理掉烦人的日志
        reason = re.sub(r" \(caused by.*?\)", "", reason)
        return f"[{extractor}] 解析失败: {reason}"

    # 兜底
    if len(msg) > 100:
        return f"未知解析错误: {msg[:100]}..."

    return f"发生错误: {msg}"
