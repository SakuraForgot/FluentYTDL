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
    import datetime as _dt

    # ── 建立文件级日志（PyInstaller console=False 时 stdout=None，print 全丢）──
    _log_path = os.path.join(cache_dir, "webview_subprocess.log")
    os.makedirs(cache_dir, exist_ok=True)

    def _log(msg: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {msg}\n"
        try:
            with open(_log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception:
            pass

    def _send_and_close(data: dict) -> None:
        """向父进程发数据，刷新管道，延迟销毁窗口。"""
        try:
            cookie_queue.put(data)
            cookie_queue.close()
            cookie_queue.join_thread()
        except Exception as e:
            _log(f"⚠️ queue 发送失败: {e}")

    def _schedule_destroy(win_ref) -> None:
        """延迟 1s 销毁窗口，让管道数据先抵达父进程。"""
        import threading
        def _do():
            time.sleep(1)
            try:
                win_ref.destroy()
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True).start()

    _log(f"=== 子进程启动 === PID={os.getpid()}")
    _log(f"login_url={login_url}, cache_dir={cache_dir}, timeout={timeout}")
    _log(f"start_hidden={start_hidden}, reveal_after={reveal_after_seconds}")

    # ── 导入 pywebview ──
    try:
        import webview
        _log(f"import webview 成功: {webview.__version__ if hasattr(webview, '__version__') else '?'}")
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        error_msg = f"pywebview 加载失败: {exc}\n{tb}"
        _log(error_msg)
        _send_and_close({"error": error_msg})
        return

    # ── 创建窗口 ──
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
        _log("create_window 成功")
    except TypeError:
        window_kwargs.pop("hidden", None)
        window = webview.create_window(**window_kwargs)
        _log("create_window 成功 (fallback, 无 hidden)")

    # ── 后台轮询线程 ──
    def _background_poll(win):
        try:
            _log("🚀 _background_poll 线程已启动")
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
                        _log("🔔 已自动显示登录窗口")
                    except Exception as e:
                        _log(f"⚠️ show() 失败: {e}")

                # 步骤 1: 检查 URL
                try:
                    current_url = win.get_current_url() or ""
                except Exception as e:
                    _log(f"⚠️ get_current_url 失败: {e}")
                    break

                if "youtube.com" not in current_url:
                    _log(f"[{elapsed}s] 等待跳回 YouTube... (当前: {current_url[:80]})")
                    continue

                # 步骤 2: YouTube 域 Cookie
                try:
                    yt_cookies = win.get_cookies() or []
                except Exception as e:
                    _log(f"⚠️ get_cookies 失败: {e}")
                    continue

                yt_names = _get_cookie_names(yt_cookies)
                _log(f"[{elapsed}s] YouTube 域 {len(yt_cookies)} 个 Cookie, names={list(yt_names)[:10]}")

                if not LOGIN_INDICATOR_YT & yt_names:
                    _log(f"[{elapsed}s] 尚未检测到 LOGIN_INFO")
                    continue

                _log("🎯 检测到 LOGIN_INFO! 用户已完成登录。")

                # 步骤 3: Google 域 Cookie
                _log("📡 导航到 accounts.google.com ...")
                google_cookies = []
                try:
                    win.load_url(GOOGLE_ACCOUNT_URL)
                    time.sleep(3)
                    google_cookies = win.get_cookies() or []
                    google_names = _get_cookie_names(google_cookies)
                    _log(f"Google 域 {len(google_cookies)} 个 Cookie, names={list(google_names)[:10]}")
                except Exception as e:
                    _log(f"⚠️ 获取 Google Cookie 失败: {e}")

                # 步骤 4: 合并+格式化+回传
                all_raw = list(yt_cookies) + list(google_cookies)
                all_names = _get_cookie_names(all_raw)
                has_core = LOGIN_INDICATOR_GOOGLE & all_names
                _log(f"合并后共 {len(all_raw)} 个, core匹配={has_core}")

                formatted = _format_cookies(all_raw)
                _log(f"格式化后 {len(formatted)} 个 Cookie")

                if formatted:
                    _send_and_close({"cookies": formatted})
                    _log(f"✅ 已回传 {len(formatted)} 个 Cookie 到父进程")
                else:
                    _send_and_close({"error": "Cookie 格式化失败：提取到空列表"})
                    _log("❌ 格式化后为空")

                _schedule_destroy(win)
                return

            # 超时
            _log(f"⏳ 提取超时 ({timeout}s)")
            _send_and_close({"error": f"登录超时 ({timeout}s)，未检测到有效的登录 Cookie"})
            _schedule_destroy(win)

        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            _log(f"💥 _background_poll 未捕获异常: {exc}\n{tb}")
            try:
                _send_and_close({"error": f"子进程内部异常: {exc}\n{tb}"})
            except Exception:
                pass
            _schedule_destroy(win)

    # ── 窗口关闭事件 ──
    def _on_closed():
        _log("🚪 用户关闭了登录窗口")
        try:
            cookie_queue.put_nowait({"error": "用户关闭了登录窗口"})
            cookie_queue.close()
            cookie_queue.join_thread()
        except Exception:
            pass

    window.events.closed += _on_closed

    _log("调用 webview.start() ...")
    webview.start(
        func=_background_poll,
        args=(window,),
        private_mode=False,
        storage_path=cache_dir,
    )
    _log("webview.start() 已返回（子进程即将退出）")


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
