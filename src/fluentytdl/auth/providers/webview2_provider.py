"""
WebView2 Cookie Provider (pywebview 双进程模型)

通过 Edge WebView2 弹出安全登录沙箱，在内存中拦截 YouTube 登录 Cookie。
使用 multiprocessing 隔离 pywebview 窗口，避免与主进程 Qt/PySide6 主循环冲突。

架构:
  主进程 (Qt GUI) ──> multiprocessing.Process ──> 子进程 (pywebview)
                  <── multiprocessing.Queue   <── Cookie 轮询检测

关键实现细节:
  1. pywebview EdgeChromium 的 get_cookies() 返回 http.cookies.SimpleCookie 对象列表！
     属性访问方式: list(cookie.keys())[0] = name, cookie[name].value = value,
     cookie[name]['domain'] = domain 等（是字典接口，不是 .name / .value 属性）
  2. GetCookiesAsync(url) 按当前 URL 过滤，需分阶段导航 (youtube + google) 采集全域 Cookie
  3. 使用 private_mode=False + storage_path 做持久化 WebView2 缓存
"""

from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path
from typing import Any

from ...utils.logger import logger

# ==================== 常量 ====================

LOGIN_INDICATOR_YT = {"LOGIN_INFO"}
LOGIN_INDICATOR_GOOGLE = {"__Secure-1PSID", "SAPISID", "SID"}

DEFAULT_TIMEOUT = 300
POLL_INTERVAL = 2.5
LONG_EXPIRY_SECONDS = 365 * 24 * 3600

YOUTUBE_HOME = "https://www.youtube.com/"
GOOGLE_ACCOUNT_URL = "https://accounts.google.com/"


# ==================== SimpleCookie 解析 ====================


def _extract_simple_cookie(cookie) -> tuple[str, dict[str, Any]] | None:
    """
    从 http.cookies.SimpleCookie 对象中提取 name 和属性。

    SimpleCookie 是一个字典：
      cookie.keys() -> ['COOKIE_NAME']
      cookie['COOKIE_NAME'].value -> 'cookie_value'
      cookie['COOKIE_NAME']['domain'] -> '.google.com'
    """
    try:
        keys = list(cookie.keys())
        if not keys:
            return None

        name = keys[0]
        morsel = cookie[name]

        return name, {
            "value": morsel.value or "",
            "domain": morsel.get("domain", "") or "",
            "path": morsel.get("path", "/") or "/",
            "secure": bool(morsel.get("secure", "")),
            "expires": morsel.get("expires", ""),
            "httponly": bool(morsel.get("httponly", "")),
        }
    except Exception:
        return None


def _get_cookie_names(cookies: list) -> set[str]:
    """从 SimpleCookie 列表中提取所有 Cookie 名称。"""
    names = set()
    for c in cookies:
        result = _extract_simple_cookie(c)
        if result:
            names.add(result[0])
    return names


# ==================== 子进程入口 ====================


def _webview_subprocess(
    cookie_queue: multiprocessing.Queue,
    login_url: str,
    cache_dir: str,
    timeout: int,
    start_hidden: bool,
    reveal_after_seconds: int,
) -> None:
    """在独立子进程中运行 pywebview 登录窗口。"""
    try:
        import webview
    except ImportError:
        cookie_queue.put({"error": "pywebview 未安装，请执行: pip install pywebview"})
        return

    window_kwargs = {
        "title": "FluentYTDL - YouTube 安全登录",
        "url": login_url,
        "width": 900,
        "height": 700,
        "resizable": True,
        "text_select": False,
    }
    if start_hidden:
        window_kwargs["hidden"] = True

    try:
        window = webview.create_window(**window_kwargs)
    except TypeError:
        # 兼容不支持 hidden 参数的 pywebview 版本
        window_kwargs.pop("hidden", None)
        window = webview.create_window(**window_kwargs)

    def _background_poll(win):
        """webview.start(func=...) 在独立线程中运行的后台轮询。"""
        print("[WebView2-子进程] 🚀 启动后台 Cookie 监视线程...")
        time.sleep(3)

        start_time = time.time()
        revealed = not start_hidden

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            time.sleep(POLL_INTERVAL)

            if start_hidden and (not revealed) and elapsed >= reveal_after_seconds:
                try:
                    win.show()
                    revealed = True
                    print("[WebView2-子进程] 🔔 后台提取未命中，已自动显示登录窗口")
                except Exception as e:
                    print(f"[WebView2-子进程] ⚠️ 显示窗口失败: {e}")

            # ---- 步骤 1: 检查当前 URL ----
            try:
                current_url = win.get_current_url() or ""
            except Exception as e:
                print(f"[WebView2-子进程] ⚠️ 获取 URL 失败: {e}")
                break

            if "youtube.com" not in current_url:
                print(
                    f"[WebView2-子进程] [{elapsed}s] 等待跳回 YouTube... (当前: {current_url[:50]})"
                )
                continue

            # ---- 步骤 2: 在 YouTube 域采集 Cookie ----
            try:
                yt_cookies = win.get_cookies() or []
            except Exception as e:
                print(f"[WebView2-子进程] ⚠️ 获取 YouTube Cookie 失败: {e}")
                continue

            yt_names = _get_cookie_names(yt_cookies)
            print(
                f"[WebView2-子进程] [{elapsed}s] YouTube 域拿到 {len(yt_cookies)} 个 Cookie: {list(yt_names)[:8]}..."
            )

            if not LOGIN_INDICATOR_YT & yt_names:
                print(f"[WebView2-子进程] [{elapsed}s] 尚未检测到 LOGIN_INFO，继续等待...")
                continue

            print("[WebView2-子进程] 🎯 检测到 LOGIN_INFO! 用户已完成登录。")

            # ---- 步骤 3: 导航到 Google 采集 .google.com 域 Cookie ----
            print("[WebView2-子进程] 📡 切换到 accounts.google.com 采集核心凭证...")
            google_cookies = []
            try:
                win.load_url(GOOGLE_ACCOUNT_URL)
                time.sleep(3)
                google_cookies = win.get_cookies() or []
                google_names = _get_cookie_names(google_cookies)
                print(
                    f"[WebView2-子进程] Google 域拿到 {len(google_cookies)} 个 Cookie: {list(google_names)[:8]}..."
                )
            except Exception as e:
                print(f"[WebView2-子进程] ⚠️ 获取 Google Cookie 失败: {e}")

            # ---- 步骤 4: 合并 + 格式化 + 回传 ----
            all_raw = list(yt_cookies) + list(google_cookies)
            all_names = _get_cookie_names(all_raw)
            has_core = LOGIN_INDICATOR_GOOGLE & all_names

            if has_core:
                print(f"[WebView2-子进程] 🎉 成功拦截到核心凭证! 匹配: {has_core}")
            else:
                print(
                    f"[WebView2-子进程] ⚠️ 未拦截到 SAPISID/__Secure-1PSID，仍尝试回传 (总计 {len(all_raw)} 个)"
                )

            formatted = _format_cookies(all_raw)

            # ★ 先 put 再 destroy
            if formatted:
                cookie_queue.put({"cookies": formatted})
                print(f"[WebView2-子进程] ✅ 已通过 Queue 回传 {len(formatted)} 个格式化 Cookie")
            else:
                cookie_queue.put({"error": "Cookie 格式化失败：提取到空列表"})

            try:
                win.destroy()
            except Exception:
                pass
            return

        # 超时
        print(f"[WebView2-子进程] ⏳ 提取超时 ({timeout}s)!")
        try:
            cookie_queue.put_nowait({"error": f"登录超时 ({timeout}s)，未检测到有效的登录 Cookie"})
        except Exception:
            pass
        try:
            win.destroy()
        except Exception:
            pass

    def _on_closed():
        print("[WebView2-子进程] 🚪 用户关闭了登录窗口")
        try:
            cookie_queue.put_nowait({"error": "用户关闭了登录窗口"})
        except Exception:
            pass

    window.events.closed += _on_closed

    webview.start(
        func=_background_poll,
        args=(window,),
        private_mode=False,
        storage_path=cache_dir,
    )


def _format_cookies(raw_cookies: list) -> list[dict[str, Any]]:
    """
    将 pywebview 返回的 SimpleCookie 列表转换为统一字典格式。
    去重: 同 name+domain 只保留最后一个。
    """
    import calendar
    import email.utils

    now = int(time.time())
    long_expiry = now + LONG_EXPIRY_SECONDS
    seen: dict[str, dict[str, Any]] = {}

    for cookie in raw_cookies:
        result = _extract_simple_cookie(cookie)
        if not result:
            continue

        name, attrs = result
        value = attrs["value"]
        domain = attrs["domain"]
        path = attrs["path"]
        secure = attrs["secure"]

        # expires 解析: SimpleCookie 的 expires 是 HTTP 日期字符串 (如 "Thu, 01 Jan 2026 00:00:00 GMT")
        raw_expires = attrs["expires"]
        expires = long_expiry  # 默认补充长有效期

        if raw_expires and isinstance(raw_expires, str) and raw_expires.strip():
            try:
                parsed = email.utils.parsedate(raw_expires)
                if parsed:
                    expires = calendar.timegm(parsed)
            except Exception:
                pass
        elif isinstance(raw_expires, (int, float)) and raw_expires > 0:
            expires = int(raw_expires)

        # 确保 domain 以 . 开头
        if domain and not domain.startswith("."):
            domain = "." + domain

        dedup_key = f"{name}|{domain}"
        seen[dedup_key] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path,
            "secure": secure,
            "expires": expires,
        }

    return list(seen.values())


# ==================== 主进程端提供者 ====================


class WebView2CookieProvider:
    """
    WebView2 Cookie 提取器 (pywebview + multiprocessing 双进程模型)
    替代旧的 DLEProvider (Deno + CDP + 临时扩展)。
    """

    def extract_cookies(
        self,
        platform: str = "youtube",
        timeout: int = DEFAULT_TIMEOUT,
        storage_path: str | None = None,
        session_tag: str | None = None,
        start_hidden: bool = False,
        reveal_after_seconds: int = 8,
    ) -> list[dict[str, Any]] | None:
        """启动 WebView2 登录窗口并提取 Cookie。"""
        login_url = YOUTUBE_HOME

        cache_dir = storage_path or str(
            Path(os.environ.get("LOCALAPPDATA", os.getcwd())) / "FluentYTDL" / ".webview_profile"
        )
        os.makedirs(cache_dir, exist_ok=True)

        session_label = session_tag or "default"

        logger.info(
            f"[WebView2] 启动安全登录窗口: {login_url} (session={session_label}, hidden_first={start_hidden})"
        )
        logger.info(f"[WebView2] WebView2 缓存目录: {cache_dir}")

        cookie_queue: multiprocessing.Queue = multiprocessing.Queue()

        process = multiprocessing.Process(
            target=_webview_subprocess,
            args=(cookie_queue, login_url, cache_dir, timeout, start_hidden, reveal_after_seconds),
            daemon=True,
        )

        try:
            process.start()
            logger.info(
                f"[WebView2] 子进程已启动 (PID: {process.pid}, session={session_label})，等待用户登录..."
            )

            try:
                result = cookie_queue.get(timeout=timeout + 30)
            except Exception:
                result = None

            if result is None:
                logger.warning("[WebView2] 未收到子进程响应 (超时)")
                self._set_error_status("登录超时，未收到 Cookie 数据")
                return None

            if "error" in result:
                error_msg = result["error"]
                logger.warning(f"[WebView2] 子进程报告错误: {error_msg}")
                self._set_error_status(error_msg)
                return None

            cookies = result.get("cookies", [])
            if not cookies:
                logger.warning("[WebView2] 子进程返回空 Cookie 列表")
                self._set_error_status("未提取到有效的 Cookie")
                return None

            logger.info(f"[WebView2] 成功提取 {len(cookies)} 个 Cookie")
            return cookies

        except Exception as e:
            logger.exception(f"[WebView2] 提取过程异常: {e}")
            self._set_error_status(f"提取异常: {e}")
            return None

        finally:
            if process.is_alive():
                logger.info("[WebView2] 强制终止残留子进程")
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()

    @staticmethod
    def _set_error_status(message: str) -> None:
        try:
            from ..auth_service import AuthStatus, auth_service

            auth_service._last_status = AuthStatus(valid=False, message=message)
        except Exception:
            pass
