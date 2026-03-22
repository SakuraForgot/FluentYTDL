from __future__ import annotations

import re

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def translate_error(error: BaseException) -> dict:
    """将异常对象转换为用户友好的错误字典。

    返回值尽量保持稳定的 keys：title/content/suggestion/raw_error。
    """

    raw_original = str(error)
    raw = _strip_ansi(raw_original)
    err_msg = raw.lower()

    from .error_parser import generate_issue_url, parse_ytdlp_error

    friendly_title, friendly_content, suggests_component_update = parse_ytdlp_error(raw)

    issue_url = generate_issue_url(
        friendly_title if friendly_title != "解析或下载失败" else "发生未知错误", raw
    )

    result = {
        "title": friendly_title if friendly_title != "解析或下载失败" else "发生未知错误",
        "content": friendly_content,
        "suggestion": "1. 请重试\n2. 查看日志文件\n3. 将此错误反馈给开发者",
        "raw_error": raw,
        "issue_url": issue_url,
        "suggests_component_update": suggests_component_update,
    }

    # 0) 链接无效/不支持
    if "unsupported url" in err_msg or "not a valid url" in err_msg:
        result["title"] = "链接无效或不受支持"
        result["content"] = "该链接不是可识别的 YouTube 视频/播放列表链接，或链接参数格式不正确。"
        result["suggestion"] = (
            "1. 重新从浏览器地址栏复制完整链接（建议以 https://www.youtube.com/watch?v=... 开头）。\n"
            "2. 如包含时间参数 t=，请确保是有效值（例如 t=30s 或 t=90）。\n"
            "3. 若是短链接 youtu.be/xxxx 也可尝试；否则请换一个链接重试。"
        )
        return result

    # 1) FFmpeg 缺失（比“后处理失败”更具体，需要优先判断）
    if (
        "ffmpeg not found" in err_msg
        or "ffprobe not found" in err_msg
        or ("ffmpeg" in err_msg and "not found" in err_msg)
        or ("ffmpeg" in err_msg and "no such file" in err_msg)
    ):
        result["title"] = "组件缺失 (FFmpeg)"
        result["content"] = "无法进行视频/音频合并或后处理，系统未检测到 FFmpeg。"
        result["suggestion"] = (
            "1. 确保已安装 FFmpeg 并添加到系统环境变量。\n"
            "2. 或在设置中指定 ffmpeg.exe 路径（设置 -> 核心组件）。"
        )
        return result

    # 2) 账号/风控类（通常伴随 403 / forbidden / not a bot / poToken）
    if (
        "confirm you are not a bot" in err_msg
        or "not a bot" in err_msg
        or "login required" in err_msg
        or "http error 403" in err_msg
        or " 403" in err_msg
        or "forbidden" in err_msg
        or "potoken" in err_msg
    ):
        result["title"] = "访问被拒绝 (403/风控)"
        result["content"] = (
            "YouTube 拒绝了请求，通常是因为节点本身被限禁或面临人机验证 (如 poToken 挑战)。"
        )
        result["suggestion"] = (
            "1. 【强烈推荐】点击底部进行『一键更新 yt-dlp 组件』。\n"
            "2. 尝试更新 Cookies（设置 -> 核心组件 -> Cookies 来源）。\n"
            "3. 暂时关闭软件，等待 30 分钟后重试或更换代理节点。"
        )
        result["suggests_component_update"] = True
        return result

    # 3) 需要登录/权限不足（与 403 风控区分开）
    if (
        "login required" in err_msg
        or ("sign in" in err_msg and ("private" in err_msg or "granted access" in err_msg))
        or "members-only" in err_msg
        or "this video is private" in err_msg
    ):
        result["title"] = "需要登录/权限不足"
        result["content"] = "该视频需要登录账号，或你没有访问权限（例如私享/会员专享）。"
        result["suggestion"] = (
            "1. 在浏览器中确认你已登录且能播放该视频。\n"
            "2. 如为私享视频，需视频作者授权访问。\n"
            "3. 如使用 Cookies，尝试更新 Cookies 后重试。"
        )
        return result

    # 4) 网络连接类
    if (
        "network is unreachable" in err_msg
        or "timed out" in err_msg
        or "connection refused" in err_msg
        or ("unable to download" in err_msg and "timed out" in err_msg)
        or "transporterror" in err_msg
    ):
        result["title"] = "网络连接异常/超时"
        result["content"] = "无法连接到 YouTube 服务器或连接不稳定。"
        result["suggestion"] = (
            "1. 检查“设置 -> 网络连接”中的代理配置是否正确。\n"
            "2. 确保代理软件（如 v2ray/clash）已开启且节点可用。\n"
            "3. 尝试在浏览器中打开该视频，确认网络可访问。"
        )
        return result

    # 5) 视频无效类
    if "video unavailable" in err_msg or "private video" in err_msg:
        result["title"] = "视频无法解析"
        result["content"] = "该视频可能已被删除、设为私享，或在你所在的地区不可用。"
        result["suggestion"] = "1. 检查链接是否正确。\n2. 确保你在浏览器中可以正常播放该视频。"
        return result

    # 6) 后处理失败（FFmpeg 存在但处理异常/格式不兼容等）
    if "postprocessing" in err_msg or "postprocess" in err_msg:
        result["title"] = "后处理失败"
        result["content"] = "下载完成后在合并/封装/转码等后处理阶段失败。"
        result["suggestion"] = (
            "1. 检查 FFmpeg 是否可用（设置 -> 核心组件 -> FFmpeg 路径）。\n"
            "2. 尝试切换下载格式/容器后重试（例如 webm/mp4）。\n"
            "3. 查看日志文件获取更详细原因。"
        )
        return result

    # 7) 其他包含 ffmpeg 但不属于“缺失”的情况
    if "ffmpeg" in err_msg:
        result["title"] = "后处理失败"
        result["content"] = "FFmpeg 在处理过程中发生错误。"
        result["suggestion"] = "1. 查看日志文件。\n2. 尝试更换格式后重试。"
        return result

    # 8) From our global error parser
    if friendly_title != "解析或下载失败":
        result["suggestion"] = ""

    return result
