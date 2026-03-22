import re
from dataclasses import dataclass
from enum import Enum


class ErrorCategory(Enum):
    """错误分类"""

    COOKIE = "cookie"  # 确定是 Cookie / 身份验证问题
    NETWORK = "network"  # 确定是网络连接问题
    AMBIGUOUS = "ambiguous"  # 403 等模糊情况，需要探测
    OTHER = "other"  # 其他错误


@dataclass
class ErrorDefinition:
    keywords: list[str]
    title: str
    description: str
    action: str
    category: ErrorCategory = ErrorCategory.OTHER
    suggests_component_update: bool = False


# 常见错误特征和对应的中文提示方案
YTDLP_ERRORS = [
    # ==================== Cookie 类（确定性高） ====================
    ErrorDefinition(
        keywords=[
            "Sign in to confirm you're not a bot",
            "Sign in to confirm youre not a bot",
            "Sign in to confirm",
            "This video is only available to registered users",
            "Only images are available for download",
            "Error solving n challenge",
            "poToken",
        ],
        title="人机验证或需要更新组件",
        description="YouTube 检测到异常的大量请求，或您的下载组件已无法通过最新的反爬虫检测（如 poToken 挑战）。",
        action="建议操作：\n1. 【强烈推荐】在当前界面点击「一键检查并更新 yt-dlp」以获取最新的反爬修复补丁。\n2. 在「设置 > 账户」中获取或刷新您的浏览器 Cookie。\n3. 当前 IP 节点可能受限，尝试更换代理节点。",
        category=ErrorCategory.COOKIE,
        suggests_component_update=True,
    ),
    ErrorDefinition(
        keywords=["Members only content"],
        title="会员专属视频",
        description="这是 YouTube 频道的会员专享内容，您的账号尚未加入该频道会员，或者未加载您的会员 Cookie。",
        action="请确保使用的 Cookie 关联的账号已购买该频道会员，并在「设置」中重新提取 Cookie。",
        category=ErrorCategory.COOKIE,
    ),
    ErrorDefinition(
        keywords=["Private video"],
        title="私人视频",
        description="该视频已被上传者设置为私有，必须拥有观看权限的账号才能访问。",
        action="请确认您有权限访问，并更新 Cookie。",
        category=ErrorCategory.COOKIE,
    ),
    ErrorDefinition(
        keywords=["Sign in to confirm your age"],
        title="年龄限制 (需要登录验证)",
        description="该视频有年龄限制，必须使用已验证年龄的账号才能访问。",
        action="请在下方选择认证方式，使用已通过年龄验证的 YouTube 账号。",
        category=ErrorCategory.COOKIE,
    ),
    # ==================== 网络类（确定性高） ====================
    ErrorDefinition(
        keywords=[
            "Connection reset by peer",
            "Connection refused",
            "Connection timed out",
            "Read timed out",
            "Timed out",
        ],
        title="网络连接超时或被拒绝",
        description="无法与 YouTube 服务器建立连接，通常是网络环境问题。",
        action="请检查代理是否正常运行，或在「设置 > 网络连接」中配置代理。",
        category=ErrorCategory.NETWORK,
    ),
    ErrorDefinition(
        keywords=[
            "SSL: CERTIFICATE_VERIFY_FAILED",
            "CERTIFICATE_VERIFY_FAILED",
            "ssl.SSLCertVerificationError",
            "certificate verify failed",
        ],
        title="SSL 证书验证失败",
        description="HTTPS 连接被干扰，证书无法通过验证。可能是网络环境篡改了证书。",
        action="请检查代理软件是否正确配置了证书信任，或更换网络环境。",
        category=ErrorCategory.NETWORK,
    ),
    ErrorDefinition(
        keywords=[
            "urlopen error",
            "URLError",
            "Name or service not known",
            "getaddrinfo failed",
            "Errno 11001",
        ],
        title="DNS 解析失败",
        description="无法解析 YouTube 的域名，通常是 DNS 被污染或网络不通。",
        action="请检查网络连接和代理配置，确保能正常访问国际网站。",
        category=ErrorCategory.NETWORK,
    ),
    ErrorDefinition(
        keywords=[
            "HTTP Error 429",
            "429 Too Many Requests",
            "Too Many Requests",
        ],
        title="请求频率过高 (429 限流)",
        description="YouTube 检测到短时间内请求过多，进行了临时限流保护。通常在停止请求后的 2-12 小时内（或次日）会自动恢复。",
        action="请暂停下载，等待 2-12 小时后重试。如急需下载，请尝试重启光猫/路由器获取新 IP，或更换代理软件的节点。",
        category=ErrorCategory.NETWORK,
    ),
    ErrorDefinition(
        keywords=[
            "proxy",
            "ProxyError",
            "Cannot connect to proxy",
            "SOCKSHTTPSConnectionPool",
        ],
        title="代理连接失败",
        description="无法连接到配置的代理服务器。",
        action="请检查代理软件是否正在运行，以及「设置 > 网络连接」中的代理地址是否正确。",
        category=ErrorCategory.NETWORK,
    ),
    # ==================== 模糊类 (403) ====================
    ErrorDefinition(
        keywords=["HTTP Error 403"],
        title="访问被拒绝 (403)",
        description="YouTube 拒绝了此请求。这可能是组件反爬逻辑过期，也可能是网络节点被临时封锁（解封通常需等待 24 小时）。",
        action="可以尝试一键更新 yt-dlp 组件，或更换网络节点。",
        category=ErrorCategory.AMBIGUOUS,
        suggests_component_update=True,
    ),
    # ==================== 其他类 ====================
    ErrorDefinition(
        keywords=["Video unavailable in your country", "Geo-restricted"],
        title="地区限制 (视频在此国家不可用)",
        description="由于版权或区域限制，当前网络节点无法访问该视频。",
        action="请尝试开启代理，并在「设置 > 网络连接」中配置正确的代理地址，或更换代理节点。",
        category=ErrorCategory.NETWORK,
    ),
    ErrorDefinition(
        keywords=["Premiere"],
        title="首映未开始",
        description="该视频属于首映状态，尚未正式开启播放或您没有观看权限。",
        action="请等待视频正式首映后再尝试下载。",
    ),
    ErrorDefinition(
        keywords=["Requested format is not available"],
        title="无可用视频流",
        description="选择的画质、音质或格式在当前视频中不存在。",
        action="请尝试降低清晰度，或选择「自动」模式重新解析。",
    ),
    ErrorDefinition(
        keywords=["ffprobe/ffmpeg not found", "ffmpeg isn't installed"],
        title="缺少核心组件 (FFmpeg)",
        description="视频合并或封面处理需要 FFmpeg，但系统未找到该工具。",
        action="请进入「设置 > 核心组件」，点击安装或更新 FFmpeg。",
    ),
    ErrorDefinition(
        keywords=["No space left on device"],
        title="磁盘空间不足",
        description="当前下载目录所在的分区没有足够的剩余空间。",
        action="请清理磁盘空间，或者在「设置 > 下载选项」中更换下载保存路径。",
    ),
]


def classify_error(error_msg: str) -> ErrorCategory:
    """
    对 yt-dlp 原始错误进行分类，返回错误类别。

    Returns:
        ErrorCategory.COOKIE / NETWORK / AMBIGUOUS / OTHER
    """
    if not error_msg:
        return ErrorCategory.OTHER
    clean = " ".join(error_msg.splitlines()).lower()

    for err_def in YTDLP_ERRORS:
        for keyword in err_def.keywords:
            if keyword.lower() in clean:
                return err_def.category

    return ErrorCategory.OTHER


def parse_ytdlp_error(error_msg: str) -> tuple[str, str, bool]:
    """
    解析 yt-dlp 或 ffmpeg 的原始错误日志，返回对用户友好的 (标题, 描述, 是否建议更新组件) 元组。
    """
    if not error_msg:
        return "未知错误", "解析过程中发生未知错误。", False

    # 清理掉换行，方便匹配
    clean_msg = " ".join(error_msg.splitlines())

    for err_def in YTDLP_ERRORS:
        for keyword in err_def.keywords:
            # 忽略大小写匹配
            if keyword.lower() in clean_msg.lower():
                detail = f"{err_def.description}\n\n建议操作：{err_def.action}"
                return err_def.title, detail, err_def.suggests_component_update

    # 兜底：未知错误，尽量提取 ERROR: 后面的内容
    match = re.search(r"ERROR:\s*(.*?)(?:\n|$)", error_msg, flags=re.IGNORECASE)
    if match:
        extracted = match.group(1).strip()
        # 限制长度
        if len(extracted) > 100:
            extracted = extracted[:97] + "..."
        return (
            "解析失败",
            f"系统遇到无法识别的错误：\n{extracted}\n\n建议尝试更新核心组件，或检查链接是否有效。",
            False,
        )

    # 如果连 ERROR 关键字都没找到，直接返回前 100 个字符
    fallback = error_msg.strip()
    if len(fallback) > 100:
        fallback = fallback[:97] + "..."
    return "解析或下载失败", f"原始信息：\n{fallback}", False


def probe_youtube_connectivity(timeout: float = 5.0) -> bool:
    """
    HEAD 请求 youtube.com 检测网络连通性（不经过 yt-dlp）。
    会自动读取应用内代理配置。

    Returns:
        True = 网络可达, False = 网络不通
    """
    import urllib.request

    try:
        from ..core.config_manager import config_manager

        proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
        proxy_url = str(config_manager.get("proxy_url", "") or "").strip()
    except Exception:
        proxy_mode = "off"
        proxy_url = ""

    try:
        req = urllib.request.Request(
            "https://www.youtube.com/",
            method="HEAD",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        )

        handlers: list = []
        if proxy_mode == "manual" and proxy_url:
            lower = proxy_url.lower()
            if not (
                lower.startswith("http://")
                or lower.startswith("https://")
                or lower.startswith("socks5://")
            ):
                proxy_url = "http://" + proxy_url
            handlers.append(urllib.request.ProxyHandler({"https": proxy_url, "http": proxy_url}))
        elif proxy_mode == "system":
            pass  # 默认使用系统代理
        else:
            handlers.append(urllib.request.ProxyHandler({}))  # 无代理

        opener = urllib.request.build_opener(*handlers)
        resp = opener.open(req, timeout=timeout)
        return resp.status < 400
    except Exception:
        return False


def _run_ytdlp_probe(cookie_file: str | None = None, timeout: float = 15.0) -> dict:
    """
    内部工具：用 yt-dlp 解析一个已知公开视频，返回结果。

    Args:
        cookie_file: Cookie 文件路径，None 表示不使用 Cookie
        timeout: 超时秒数

    Returns:
        {"ok": bool, "category": ErrorCategory, "stderr": str, "latency_ms": int}
    """
    import subprocess
    import time as _time

    try:
        from ..youtube.yt_dlp_cli import prepare_yt_dlp_env, resolve_yt_dlp_exe

        exe = resolve_yt_dlp_exe()
        if exe is None:
            return {
                "ok": False,
                "category": ErrorCategory.OTHER,
                "stderr": "yt-dlp 未安装",
                "latency_ms": -1,
            }

        env = prepare_yt_dlp_env()

        # 使用一个真实的、需要一定权限/容易触发风控的视频作为探针
        import random

        from ..core.config_manager import config_manager

        # 获取历史记录和链接池配置
        pool = config_manager.get("probe_link_pool")
        if not isinstance(pool, list) or not pool:
            # Fallback hardcoded if invalid
            pool = ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]

        history = config_manager.get("probe_link_history", {})
        if not isinstance(history, dict):
            history = {}

        current_time = _time.time()
        available_links = []

        # 过滤出 12 小时内未使用过的链接
        for link in pool:
            last_used = history.get(link, 0)
            if current_time - last_used > 12 * 3600:
                available_links.append(link)

        if available_links:
            test_url = random.choice(available_links)
        else:
            # 如果所有的都被用过了（池子较小），则挑选距离现在最久远的那个
            sorted_history = sorted(
                [(link, history.get(link, 0)) for link in pool], key=lambda x: x[1]
            )
            test_url = sorted_history[0][0]

        # 更新该链接的历史记录并保存
        history[test_url] = current_time
        config_manager.set("probe_link_history", history)

        cmd = [
            str(exe),
            "--dump-json",
            "--no-cache-dir",
            "--no-warnings",
            "--socket-timeout",
            "10",
            test_url,
        ]

        if cookie_file:
            cmd.extend(["--cookies", cookie_file])

        # 代理配置
        try:
            from ..core.config_manager import config_manager

            proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
            proxy_url = str(config_manager.get("proxy_url", "") or "").strip()
            if proxy_mode == "manual" and proxy_url:
                cmd.extend(["--proxy", proxy_url])
        except Exception:
            pass

        start = _time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW
            if hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0,
        )
        latency_ms = int((_time.monotonic() - start) * 1000)

        if proc.returncode == 0:
            return {
                "ok": True,
                "category": ErrorCategory.OTHER,
                "stderr": "",
                "latency_ms": latency_ms,
            }

        stderr = proc.stderr or ""
        return {
            "ok": False,
            "category": classify_error(stderr),
            "stderr": stderr,
            "latency_ms": latency_ms,
        }

    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "category": ErrorCategory.NETWORK,
            "stderr": f"超时 ({timeout}s)",
            "latency_ms": int(timeout * 1000),
        }
    except Exception as e:
        return {"ok": False, "category": ErrorCategory.OTHER, "stderr": str(e), "latency_ms": -1}


def probe_cookie_and_ip(cookie_file: str | None = None, timeout: float = 15.0) -> dict:
    """
    综合探测 Cookie 有效性 + IP 风控状态。

    策略：
    1. 先带 Cookie 解析 → 成功 = Cookie 有效 + IP 正常，结束
    2. 失败 → 不带 Cookie 解析 → 对比结果确定根因

    Returns:
        {
            "cookie_ok": bool,        # Cookie 是否有效
            "ip_ok": bool,            # IP 是否无风控
            "detail": str,            # 详细描述
            "latency_ms": int,        # 总耗时
            "with_cookie": dict,      # 带 Cookie 探测原始结果
            "without_cookie": dict | None,  # 不带 Cookie 探测原始结果（仅失败时有）
        }
    """
    import time as _time

    start = _time.monotonic()

    # ====== 第 1 步：带 Cookie 探测 ======
    if cookie_file:
        r1 = _run_ytdlp_probe(cookie_file=cookie_file, timeout=timeout)
    else:
        # 没有 Cookie 文件就直接跳到无 Cookie 探测
        r1 = {
            "ok": False,
            "category": ErrorCategory.OTHER,
            "stderr": "无 Cookie 文件",
            "latency_ms": 0,
        }

    if r1["ok"]:
        # 成功 → Cookie 有效 + IP 正常
        total_ms = int((_time.monotonic() - start) * 1000)
        return {
            "cookie_ok": True,
            "ip_ok": True,
            "detail": f"✅ Cookie 有效且 IP 无风控 ({r1['latency_ms']}ms)",
            "latency_ms": total_ms,
            "with_cookie": r1,
            "without_cookie": None,
        }

    # ====== 第 2 步：不带 Cookie 探测（用于对比） ======
    r2 = _run_ytdlp_probe(cookie_file=None, timeout=timeout)
    total_ms = int((_time.monotonic() - start) * 1000)

    #  交叉判定表：
    #  r1(有cookie) | r2(无cookie) | 结论
    #  ─────────────┼──────────────┼─────────────────
    #  失败 COOKIE  | 成功         | Cookie 失效，IP 正常
    #  失败 COOKIE  | 失败 COOKIE  | IP 被风控 (两者都触发验证)
    #  失败 NETWORK | 失败 NETWORK | 网络不通
    #  失败 AMBIG   | 成功         | Cookie 问题导致 403
    #  失败 AMBIG   | 失败         | IP 被封

    if r2["ok"]:
        # 无 Cookie 能过 → Cookie 是问题所在
        return {
            "cookie_ok": False,
            "ip_ok": True,
            "detail": "❌ Cookie 无效 — 不带 Cookie 可正常解析，IP 无风控",
            "latency_ms": total_ms,
            "with_cookie": r1,
            "without_cookie": r2,
        }

    # 两者都失败
    if r1["category"] == ErrorCategory.NETWORK or r2["category"] == ErrorCategory.NETWORK:
        return {
            "cookie_ok": False,
            "ip_ok": False,
            "detail": f"❌ 网络不通: {r2['stderr'][:60]}",
            "latency_ms": total_ms,
            "with_cookie": r1,
            "without_cookie": r2,
        }

    # 两者都触发身份验证或 403 → IP 被风控
    return {
        "cookie_ok": False,
        "ip_ok": False,
        "detail": "❌ IP 被风控 (有无 Cookie 均触发验证)，建议更换代理节点",
        "latency_ms": total_ms,
        "with_cookie": r1,
        "without_cookie": r2,
    }


def probe_ip_risk_control(timeout: float = 15.0) -> dict:
    """
    仅检测 IP 风控（无 Cookie），向后兼容。

    Returns:
        {"blocked": bool, "detail": str, "latency_ms": int}
    """
    r = _run_ytdlp_probe(cookie_file=None, timeout=timeout)
    if r["ok"]:
        return {"blocked": False, "detail": "未检测到风控", "latency_ms": r["latency_ms"]}
    if r["category"] in (ErrorCategory.COOKIE, ErrorCategory.AMBIGUOUS):
        return {
            "blocked": True,
            "detail": "触发身份验证 (IP 被风控)",
            "latency_ms": r["latency_ms"],
        }
    if r["category"] == ErrorCategory.NETWORK:
        return {
            "blocked": True,
            "detail": f"网络错误: {r['stderr'][:80]}",
            "latency_ms": r["latency_ms"],
        }
    short = r["stderr"].strip()[:100] if r["stderr"].strip() else "未知错误"
    return {"blocked": False, "detail": f"解析异常但非风控: {short}", "latency_ms": r["latency_ms"]}


def generate_issue_url(title: str, raw_error: str) -> str:
    """
    根据错误标题和原始日志，生成预填内容的 GitHub Issue 链接。
    """
    import urllib.parse

    # 限制原日志长度，防止 URL 过长导致浏览器拒绝 (通常 URL 限制在 2000-8000 字符)
    max_err_len = 1500
    if len(raw_error) > max_err_len:
        raw_error = raw_error[:max_err_len] + "\n...[Truncated]"

    issue_title = urllib.parse.quote(f"[AutoReport] {title}")

    body = (
        "### 错误描述\n"
        "自动捕获到的错误：\n"
        f"**{title}**\n\n"
        "### 错误日志\n"
        "```text\n"
        f"{raw_error}\n"
        "```\n\n"
        "### 其他信息\n"
        "- FluentYTDL 版本: \n"
        "- 操作系统: \n"
    )
    issue_body = urllib.parse.quote(body)

    # 附带 labels=bug
    return f"https://github.com/prideicker/FluentYTDL/issues/new?title={issue_title}&body={issue_body}&labels=bug"
