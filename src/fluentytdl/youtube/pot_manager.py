"""
POT (Proof of Origin Token) Provider Manager

管理 bgutil-ytdlp-pot-provider 服务的生命周期，为 yt-dlp 提供 PO Token 以绕过 YouTube 的机器人检测。

核心功能：
- 动态端口分配
- 健康检查
- 僵尸进程管理
- Windows Job Objects 支持
"""

from __future__ import annotations

import atexit
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from loguru import logger

from ..utils.paths import find_bundled_executable, frozen_app_dir, get_clean_env


class POTManager:
    """PO Token 服务管理器（单例）"""

    _instance: POTManager | None = None

    # 端口范围
    DEFAULT_PORT = 4416
    PORT_RANGE = 10

    def __new__(cls) -> POTManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._process: subprocess.Popen | None = None
        self._active_port: int = 0
        self._is_running: bool = False
        self._job_handle: int | None = None  # Windows Job Object
        self._lock = threading.Lock()
        self._warm_event = threading.Event()  # 预热完成信号
        self._warm_thread: threading.Thread | None = None  # 后台预热线程
        self._warm_attempts: int = 0  # 预热失败次数（退避用）
        self._warm_retry_at: float = 0.0  # 下次允许重试的 monotonic 时间
        self._last_token_len: int = 0  # 最近一次成功铸出的 Token 长度（只记长度）
        self._last_minter_size: int | None = None  # 最近一次读到的 minter 缓存条目数

        # 注册退出清理
        atexit.register(self.stop_server)

        # Windows: 设置 Job Object
        if sys.platform == "win32":
            self._setup_job_object()

    def _setup_job_object(self):
        """创建 Windows Job Object，确保子进程随父进程终止"""
        try:
            import win32job

            # 必须匿名。具名 job（曾用 "FluentYTDL_POT_Job"）会被同名进程共享：
            # CreateJobObject 遇到已存在的名字时返回*现有* job 的句柄，而
            # KILL_ON_JOB_CLOSE 只在最后一个句柄关闭时才触发。于是两个实例同时跑
            # 时，A 被强杀后 B 仍持句柄 → A 的 POT 服务变成孤儿进程活下来。
            # 匿名 job 每进程独占，句柄随进程消亡，强杀也能带走子进程。
            # 名字传 ""，不能传 None —— pywin32 会抛 "None is not a valid string"，
            # 于是 _job_handle 变 None、进程根本没被纳管，比具名还糟。
            job_handle = win32job.CreateJobObject(None, "")
            if job_handle is None:
                raise RuntimeError("CreateJobObject returned None")
            info = win32job.QueryInformationJobObject(
                job_handle, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job_handle, win32job.JobObjectExtendedLimitInformation, info
            )
            self._job_handle = job_handle
            logger.debug("POT Manager: Windows Job Object 已创建")
        except Exception as e:
            logger.warning(f"POT Manager: 创建 Job Object 失败: {e}")
            self._job_handle = None

    def _find_available_port(self) -> int:
        """查找可用端口"""
        for offset in range(self.PORT_RANGE):
            port = self.DEFAULT_PORT + offset
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return port

        # 兜底：让 OS 分配
        with socket.socket() as s:
            s.bind(("", 0))
            return s.getsockname()[1]

    def _find_pot_executable(self) -> Path | None:
        """查找 POT Provider 可执行文件"""
        candidates = [
            "pot-provider/bgutil-pot-provider.exe",
            "bgutil-pot-provider.exe",
            "pot-provider/bgutil-ytdlp-pot-provider.exe",
            "bgutil-ytdlp-pot-provider.exe",
        ]

        for candidate in candidates:
            exe = find_bundled_executable(candidate)
            if exe and exe.exists():
                return exe

        # 尝试 frozen_app_dir 下的 bin 目录
        app_dir = frozen_app_dir()
        for subdir in ["bin/pot-provider", "pot-provider", "bin"]:
            for name in ["bgutil-pot-provider.exe", "bgutil-ytdlp-pot-provider.exe"]:
                p = app_dir / subdir / name
                if p.exists():
                    return p

        return None

    @staticmethod
    def _local_urlopen(req, timeout=5.0):
        """对本地 127.0.0.1 的请求，绕过系统/TUN 代理

        TUN 模式（如 V2RayN）会劫持所有流量包括 localhost，
        导致对本地 POT 服务的请求被送到代理服务器然后超时。
        使用空 ProxyHandler 创建无代理 opener 绕过此问题。
        """
        import urllib.request

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)

    def _cleanup_orphan_servers(self):
        """清理残留的 POT 服务进程"""
        import urllib.request

        for port in range(self.DEFAULT_PORT, self.DEFAULT_PORT + self.PORT_RANGE):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/shutdown", method="POST")
                self._local_urlopen(req, timeout=0.3)
                logger.info(f"POT Manager: 已关闭残留服务 (端口 {port})")
            except Exception:
                pass

    def _health_check(self, timeout: float = 3.0) -> bool:
        """健康检查：确认服务端口已就绪"""
        start = time.time()
        while time.time() - start < timeout:
            try:
                # 使用 socket 检测端口是否在监听
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    result = s.connect_ex(("127.0.0.1", self._active_port))
                    if result == 0:
                        # 端口已在监听
                        logger.debug(f"POT Manager: 端口 {self._active_port} 已就绪")
                        return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def start_server(self) -> bool:
        """启动 POT 服务"""
        with self._lock:
            if self._is_running and self._process and self._process.poll() is None:
                logger.debug("POT Manager: 服务已在运行")
                return True

            # 查找可执行文件
            exe = self._find_pot_executable()
            if not exe:
                logger.warning("POT Manager: 未找到 POT Provider 可执行文件")
                return False

            # 清理残留
            self._cleanup_orphan_servers()

            # 查找可用端口
            self._active_port = self._find_available_port()
            logger.info(f"POT Manager: 使用端口 {self._active_port}")

            try:
                # bgutil-pot-provider 的命令行参数格式
                # 显式指定 --host 127.0.0.1 以确保监听 IPv4
                cmd = [
                    str(exe),
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self._active_port),
                    "--verbose",
                ]

                # --- 注入代理配置 ---
                # bgutil-pot-provider 需要通过代理访问 Google BotGuard API
                # 支持 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY 环境变量
                env = get_clean_env()
                try:
                    from ..core.config_manager import config_manager

                    proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
                    proxy_url = str(config_manager.get("proxy_url") or "").strip()

                    if proxy_mode in ("http", "socks5") and proxy_url:
                        # 手动代理：确保有 scheme
                        lower = proxy_url.lower()
                        if lower.startswith(("http://", "https://", "socks5://")):
                            proxy_full = proxy_url
                        else:
                            scheme = "socks5" if proxy_mode == "socks5" else "http"
                            proxy_full = f"{scheme}://{proxy_url}"

                        env["HTTPS_PROXY"] = proxy_full
                        env["HTTP_PROXY"] = proxy_full
                        env["ALL_PROXY"] = proxy_full
                        logger.info(f"POT Manager: 注入代理 → {proxy_full}")
                    elif proxy_mode == "system":
                        # 系统代理 / TUN 模式：直接继承环境变量，不主动注入
                        # TUN 模式（如 V2RayN）已经在网络层透明代理所有流量，
                        # 如果再注入 HTTPS_PROXY 会造成双重代理导致服务卡死。
                        logger.debug("POT Manager: 系统代理模式，继承环境（兼容 TUN）")
                    else:
                        # 无代理
                        logger.debug("POT Manager: 无代理配置")
                except Exception as e:
                    logger.debug(f"POT Manager: 读取代理配置失败: {e}")

                creation_flags = 0
                if sys.platform == "win32":
                    creation_flags = subprocess.CREATE_NO_WINDOW

                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creation_flags,
                    env=env,
                )

                # Windows: 关联到 Job Object
                if self._job_handle and sys.platform == "win32":
                    try:
                        import win32api
                        import win32con
                        import win32job

                        handle = win32api.OpenProcess(
                            win32con.PROCESS_ALL_ACCESS, False, self._process.pid
                        )
                        win32job.AssignProcessToJobObject(self._job_handle, handle)
                    except Exception as e:
                        logger.warning(f"POT Manager: 关联 Job Object 失败: {e}")

                # 健康检查（增加超时到 10 秒）
                logger.debug(f"POT Manager: 开始健康检查 (PID: {self._process.pid})")
                if self._health_check(timeout=10.0):
                    self._is_running = True
                    logger.info(
                        f"POT Manager: 服务已启动 (PID: {self._process.pid}, 端口: {self._active_port})"
                    )
                    return True
                else:
                    # 检查进程是否还在运行
                    if self._process.poll() is None:
                        # 进程活着但端口不通：绝不能标记 running。
                        # 否则 is_running() 会撒谎 → 注入一个不可用的 base_url →
                        # yt-dlp 侧白等插件的 _GETPOT_TIMEOUT(20s) 后静默降级。
                        self._is_running = False
                        logger.warning(
                            "[POT][Ready] 失败 stage=server_down "
                            f"(进程存活 pid={self._process.pid} 但端口 {self._active_port} 不通)"
                        )
                        return False
                    else:
                        # 进程已退出，获取输出以便调试
                        stdout, stderr = self._process.communicate(timeout=1)
                        if stderr:
                            logger.error(
                                f"POT Manager 进程 stderr: {stderr.decode('utf-8', errors='ignore')[:500]}"
                            )
                        if stdout:
                            logger.debug(
                                f"POT Manager 进程 stdout: {stdout.decode('utf-8', errors='ignore')[:500]}"
                            )
                        logger.error(
                            f"POT Manager: 进程已退出 (返回码: {self._process.returncode})"
                        )
                        return False

            except Exception as e:
                logger.error(f"POT Manager: 启动服务失败: {e}")
                return False

    def stop_server(self):
        """停止 POT 服务"""
        with self._lock:
            # 防止重复停止（如果进程已经不存在了）
            if not self._is_running and self._process is None:
                logger.debug("POT Manager: 服务未运行，跳过停止操作")
                return

            proc = self._process
            if proc is not None:
                try:
                    # 检查进程是否已经终止
                    if proc.poll() is not None:
                        logger.debug("POT Manager: 进程已终止，跳过停止操作")
                        self._process = None
                        self._is_running = False
                        self._active_port = 0
                        return

                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                finally:
                    self._process = None

            self._is_running = False
            self._active_port = 0
            logger.info("POT Manager: 服务已停止")

    def invalidate_caches(self) -> bool:
        """清除 POT 服务的所有内部缓存（最轻量的恢复手段）

        调用 POST /invalidate_caches 端点，让服务丢弃已缓存的 Token
        并在下次请求时重新从 BotGuard 生成新 Token。
        """
        if not self.is_running():
            return False
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._active_port}/invalidate_caches",
                method="POST",
            )
            self._local_urlopen(req, timeout=3)
            logger.info("POT Manager: 缓存已清除")
            return True
        except Exception as e:
            logger.warning(f"POT Manager: 清除缓存失败: {e}")
            return False

    def invalidate_integrity_token(self) -> bool:
        """使 Integrity Token 失效，强制重新生成

        调用 POST /invalidate_it 端点，让服务重新向 BotGuard 申请
        新的 Integrity Token，比清缓存更彻底。
        """
        if not self.is_running():
            return False
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._active_port}/invalidate_it",
                method="POST",
            )
            self._local_urlopen(req, timeout=5)
            logger.info("POT Manager: Integrity Token 已失效，将重新生成")
            return True
        except Exception as e:
            logger.warning(f"POT Manager: 使 Integrity Token 失效失败: {e}")
            return False

    def restart_server(self) -> bool:
        """完整重启 POT 服务（最彻底的恢复手段）"""
        logger.info("POT Manager: 触发完整重启...")
        self.stop_server()
        import time as _time

        _time.sleep(0.5)
        return self.start_server()

    def try_recover(self) -> bool:
        """渐进式恢复 POT 服务（Bot 检测错误时调用）

        按从轻到重的顺序尝试恢复：
        1. 清除缓存 (POST /invalidate_caches)
        2. 重置 Integrity Token (POST /invalidate_it)
        3. 完整重启服务

        每步恢复后都通过 verify_token_generation() 验证 Token 生成能力，
        确保恢复的不是"空壳"服务。

        如果服务本身未运行，则直接尝试启动。

        Returns:
            True 如果恢复成功且 Token 可正常生成
        """
        # 服务未运行 → 直接启动
        if not self.is_running():
            logger.info("POT Manager: 服务未运行，尝试启动...")
            if not self.start_server():
                return False
            # 启动后验证 Token 生成能力
            ok, _ = self.verify_token_generation()
            return ok

        # 第一步：清除缓存
        logger.info("POT Manager: 恢复步骤 1/3 — 清除缓存")
        if self.invalidate_caches():
            ok, msg = self.verify_token_generation()
            if ok:
                logger.info(f"POT Manager: 缓存清除后 Token 验证通过: {msg}")
                return True
            logger.warning(f"POT Manager: 缓存清除后 Token 仍无效: {msg}")

        # 第二步：重置 Integrity Token
        logger.info("POT Manager: 恢复步骤 2/3 — 重置 Integrity Token")
        if self.invalidate_integrity_token():
            ok, msg = self.verify_token_generation()
            if ok:
                logger.info(f"POT Manager: IT 重置后 Token 验证通过: {msg}")
                return True
            logger.warning(f"POT Manager: IT 重置后 Token 仍无效: {msg}")

        # 第三步：完整重启
        logger.info("POT Manager: 恢复步骤 3/3 — 完整重启服务")
        if not self.restart_server():
            return False
        ok, _ = self.verify_token_generation()
        return ok

    def is_running(self) -> bool:
        """检查服务是否运行中"""
        if not self._is_running:
            return False
        if self._process and self._process.poll() is not None:
            self._is_running = False
            return False
        return True

    def verify_token_generation(self, timeout: float = 15.0) -> tuple[bool, str]:
        """L1 验证：调用 POST /get_pot 检查服务能否正常产出 PO Token

        向 POT Provider 发送一个空的 Token 生成请求，验证：
        - HTTP 状态码是否为 200
        - 返回 JSON 是否包含 po_token 字段
        - po_token 是否为非空字符串

        Returns:
            (success: bool, detail: str)
        """
        if not self.is_running():
            return False, "服务未运行"

        import json
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._active_port}/get_pot",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            resp = self._local_urlopen(req, timeout=timeout)
            body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body)

            po_token = data.get("poToken") or data.get("po_token") or data.get("token") or ""
            if isinstance(po_token, str) and len(po_token) >= 16:
                # 只记长度，绝不记 Token 明文（CLAUDE.md §8 / 日志安全约束）
                self._last_token_len = len(po_token)
                self._warm_event.set()  # 标记预热完成
                return True, f"Token 有效 (长度 {len(po_token)})"
            else:
                return False, f"Token 格式异常: 长度={len(str(po_token))}"
        except urllib.error.HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except json.JSONDecodeError:
            return False, "返回内容非 JSON"
        except Exception as e:
            return False, f"请求失败: {e}"

    def check_minter_health(self, timeout: float = 3.0) -> tuple[bool, str]:
        """L2 验证：检查 Minter 缓存状态（BotGuard 铸造器健康度）

        调用 GET /minter_cache 查看 minter 是否已初始化和缓存是否正常。

        Returns:
            (healthy: bool, detail: str)
        """
        if not self.is_running():
            return False, "服务未运行"

        import json
        import urllib.error
        import urllib.request

        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._active_port}/minter_cache",
                method="GET",
            )
            resp = self._local_urlopen(req, timeout=timeout)
            body = resp.read().decode("utf-8", errors="ignore")

            # 如果返回了有效 JSON，说明 minter 至少已初始化
            data = json.loads(body)
            # 尝试提取有意义的信息
            if isinstance(data, dict):
                cache_size = data.get("size") or data.get("len") or data.get("count")
                if cache_size is None and data:
                    # 有些版本直接把缓存内容当字典返回，键数即条目数
                    cache_size = len(data)
                self._last_minter_size = self._as_int(cache_size)
                if cache_size is not None:
                    return True, f"Minter 缓存正常 (条目: {cache_size})"
                return True, "Minter 已初始化"
            elif isinstance(data, list):
                self._last_minter_size = len(data)
                return True, f"Minter 缓存正常 ({len(data)} 条目)"
            else:
                return True, "Minter 响应正常"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                # 旧版本可能不支持此端点
                return True, "端点不存在 (旧版本，跳过)"
            return False, f"HTTP {e.code}: {e.reason}"
        except json.JSONDecodeError:
            # 非 JSON 响应也可能是正常的 (取决于版本)
            return True, "Minter 响应正常 (非 JSON)"
        except Exception as e:
            return False, f"请求失败: {e}"

    @staticmethod
    def _as_int(value: object) -> int | None:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @property
    def last_minter_size(self) -> int | None:
        """最近一次读到的 minter 缓存条目数（未读到过为 None）。

        用途是交叉验证：把它和"解析/下载次数"对比，能反向确认 Token 是真被
        yt-dlp 消费了，还是根本没人请求（后者说明插件没加载或 provider 没被选中）。
        """
        return self._last_minter_size

    def get_health_status(self) -> dict:
        """综合诊断 POT 服务状态（用于一键检测）

        返回包含多层验证结果的字典：
        {
            "running": bool,           # L0: 进程存活
            "port": int,               # 活动端口
            "token_ok": bool,          # L1: 能否生成 Token
            "token_detail": str,       # L1: 详细信息
            "minter_ok": bool,         # L2: Minter 健康度
            "minter_detail": str,      # L2: 详细信息
            "overall_ok": bool,        # 综合判定
            "summary": str,            # 人类可读摘要
        }
        """
        result: dict = {
            "running": False,
            "port": self._active_port,
            "token_ok": False,
            "token_detail": "",
            "minter_ok": False,
            "minter_detail": "",
            "minter_cache_size": None,
            "token_len": self._last_token_len,
            "overall_ok": False,
            "summary": "",
        }

        # L0: 进程存活
        result["running"] = self.is_running()
        if not result["running"]:
            result["summary"] = "服务未运行"
            return result

        result["port"] = self._active_port

        # L2: Minter 健康 (先查，更快)
        minter_ok, minter_detail = self.check_minter_health()
        result["minter_ok"] = minter_ok
        result["minter_detail"] = minter_detail
        result["minter_cache_size"] = self._last_minter_size

        # L1: Token 生成能力
        token_ok, token_detail = self.verify_token_generation()
        result["token_ok"] = token_ok
        result["token_detail"] = token_detail
        result["token_len"] = self._last_token_len

        # 综合判定
        result["overall_ok"] = result["running"] and token_ok
        if result["overall_ok"]:
            result["summary"] = f"运行中 (端口 {self._active_port}), {token_detail}"
        elif result["running"] and not token_ok:
            result["summary"] = f"运行中但 Token 生成异常: {token_detail}"
        else:
            result["summary"] = f"异常: {token_detail}"

        return result

    def verify_plugin_loadable(self) -> tuple[bool, str]:
        """验证 POT 插件是否已就位于 yt-dlp.exe 旁的标准插件目录。

        独立编译的 yt-dlp.exe 不支持 PYTHONPATH 插件加载，只能通过
        <exe-dir>/yt-dlp-plugins/<pkg>/yt_dlp_plugins/extractor/ 发现插件。

        此方法检查：
        1. yt-dlp.exe 旁是否存在标准插件目录结构
        2. 插件文件是否存在

        Returns:
            (ok, message) 元组
        """
        try:
            from .yt_dlp_cli import resolve_yt_dlp_exe

            exe = resolve_yt_dlp_exe()
            if exe is None:
                return False, "yt-dlp 可执行文件未找到"

            # 检查标准插件目录
            plugin_dir = (
                exe.parent
                / "yt-dlp-plugins"
                / "bgutil-ytdlp-pot-provider"
                / "yt_dlp_plugins"
                / "extractor"
            )

            if not plugin_dir.exists():
                return False, (
                    f"POT 插件目录不存在: {plugin_dir.parent.parent}。"
                    "请确保 sync_pot_plugins_to_ytdlp() 已正确执行。"
                )

            # 检查关键插件文件
            http_plugin = plugin_dir / "getpot_bgutil_http.py"
            base_plugin = plugin_dir / "getpot_bgutil.py"

            if not http_plugin.exists():
                return False, "POT HTTP 插件文件 (getpot_bgutil_http.py) 缺失"
            if not base_plugin.exists():
                return False, "POT 基础插件文件 (getpot_bgutil.py) 缺失"

            # 全部检查通过
            plugin_files = list(plugin_dir.glob("getpot_bgutil*.py"))
            return True, f"POT 插件已就位 ({len(plugin_files)} 个文件，位于 yt-dlp.exe 旁)"

        except Exception as e:
            return False, f"插件检测异常: {e}"

    # 主动探测用的默认目标：yt-dlp 自己的测试视频，长期可用且时长极短。
    PROBE_URL = "https://www.youtube.com/watch?v=BaW_jenozKc"

    def probe_ytdlp_provider(
        self, url: str | None = None, timeout: float = 90.0
    ) -> tuple[bool, str]:
        """带 `-v` 跑一次真实解析，抓 yt-dlp 自己关于 POT provider 的输出。

        这是唯一能证明"插件被加载 + provider 被选中"的手段：bgutil 插件只在
        trace 级别打自己的日志，正常运行的 yt-dlp 输出里看不到任何痕迹，
        所以被动扫日志永远拿不到这一层证据。

        不传 cookie：诊断不该碰 cookie jar，输出也就不会夹带凭据。

        Returns:
            (ok, 证据文本)。ok=True 表示输出里出现了 bgutil provider 的痕迹。
        """
        from .yt_dlp_cli import (
            _safe_working_dir,
            _win_hide_console_kwargs,
            prepare_yt_dlp_env,
            resolve_yt_dlp_exe,
        )

        exe = resolve_yt_dlp_exe()
        if exe is None:
            return False, "yt-dlp 可执行文件未找到"

        args = self.get_extractor_args()
        if not args:
            return False, "POT 服务未运行，无法注入 base_url（先启用并等待预热）"

        cmd = [
            str(exe),
            "--no-color",
            "--no-progress",
            "--simulate",
            "--no-playlist",
            "-v",
            "--extractor-args",
            args,
            url or self.PROBE_URL,
        ]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                env=prepare_yt_dlp_env(),
                cwd=_safe_working_dir(),
                **_win_hide_console_kwargs(),
            )
        except subprocess.TimeoutExpired:
            return False, f"探测超时（>{timeout:.0f}s）"
        except Exception as e:
            return False, f"探测执行失败: {e}"

        out = (proc.stdout or b"").decode("utf-8", errors="replace")
        out += "\n" + (proc.stderr or b"").decode("utf-8", errors="replace")

        markers = ("bgutil", "getpot", "po token", "po_token", "potoken", "youtubepot")
        evidence: list[str] = []
        rejected = False
        for line in out.splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if any(m in low for m in markers):
                if "rejected" in low:
                    rejected = True
                if len(evidence) < 40:
                    evidence.append(s[:300])

        if not evidence:
            return False, (
                f"yt-dlp 输出里没有任何 POT 痕迹 —— 插件很可能没被加载。\n退出码={proc.returncode}"
            )

        text = "\n".join(evidence)
        hit_bgutil = any("bgutil" in e.lower() for e in evidence)
        if rejected:
            return False, "provider 拒绝了请求（服务不可达时会静默降级）：\n" + text
        if not hit_bgutil:
            return False, "只看到 POT 相关输出，但没有 bgutil provider 的痕迹：\n" + text
        return True, text

    def status_brief(self) -> str:
        """一行状态摘要。**只读内存状态，零网络 I/O**，可直接在 UI 线程调用。

        get_health_status() 会真去铸一次 Token（最坏 15s），绝不能拿它刷 UI。
        """
        from ..core.config_manager import config_manager

        if not config_manager.get("pot_provider_enabled", False):
            return "已关闭"
        if self.is_warm:
            extra = f"，Token 长度 {self._last_token_len}" if self._last_token_len else ""
            if self._last_minter_size is not None:
                extra += f"，minter 缓存 {self._last_minter_size}"
            return f"已就绪（端口 {self._active_port}{extra}）"
        running = self.is_running()
        # 退避判定必须先于"预热中"：断网时服务是本地进程、起得来，只有铸 Token 失败，
        # 于是 running=True 而预热线程早已退出。若先看 running 就会一直显示"预热中…"，
        # 把"正在退避、当前没人干活"说成"马上就好"。
        if self._warm_retry_at:
            remaining = self._warm_retry_at - time.monotonic()
            if remaining > 0:
                prefix = "服务已起但未就绪" if running else "未运行"
                return f"{prefix}，{remaining:.0f}s 后重试预热（已失败 {self._warm_attempts} 次）"
        if not running:
            return "未运行"
        return f"预热中…（端口 {self._active_port}，此期间解析降级为无 POT）"

    def get_extractor_args(self) -> str | None:
        """获取 yt-dlp 的 extractor-args 参数"""
        if not self.is_running():
            return None
        return f"youtubepot-bgutilhttp:base_url=http://127.0.0.1:{self._active_port}"

    @property
    def active_port(self) -> int:
        return self._active_port

    @property
    def is_warm(self) -> bool:
        """预热是否完成（至少成功生成过一次 Token）"""
        return self._warm_event.is_set()

    # 这里曾有一个 wait_until_ready(timeout=15)：等 _warm_event，超时后还会再主动跑一次
    # verify_token_generation(timeout=20) —— 最坏 35s。它已被 ensure_warm_async() 取代
    # （解析路径只读 is_warm，未就绪即降级），且全仓库无调用方，故整段删除而非留着当陷阱。
    # 需要"等就绪"的场景请用 ensure_warm_async() + 轮询 is_warm，绝不要在解析路径上等。

    # --- 后台预热（解析路径永不阻塞） ---

    _BACKOFF_SCHEDULE = (30.0, 120.0, 300.0)

    def ensure_warm_async(self) -> None:
        """触发一次后台预热，立即返回。解析路径调用此方法，绝不等待。

        - 已 warm 或已有预热线程在跑 → 直接返回
        - 失败按 30s / 2min / 5min 退避重试，不再"一次失败就永久不 warm"
        """
        if self._warm_event.is_set():
            return
        with self._lock:
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return
            if self._warm_retry_at and time.monotonic() < self._warm_retry_at:
                return
            self._warm_thread = threading.Thread(
                target=self._warm_worker, name="pot-warm", daemon=True
            )
            self._warm_thread.start()

    def _warm_worker(self) -> None:
        """daemon 线程体：拉起服务 → 铸 Token → 校验插件 → 合成一条 [POT][Ready]。"""
        t0 = time.monotonic()
        try:
            if not self.is_running() and not self.start_server():
                self._schedule_warm_retry("server_down")
                return

            ok, msg = self.verify_token_generation(timeout=20.0)
            if not ok:
                self._schedule_warm_retry("token_fail", detail=msg)
                return

            plugin_ok, plugin_msg = self.verify_plugin_loadable()
            minter_ok, minter_msg = self.check_minter_health()
            deno_ok = self._probe_deno()
            # 记条目数而非笼统的 OK：诊断时拿它和"解析/下载次数"对比，
            # 就能反向确认 Token 是真被消费，还是根本没人请求。
            if not minter_ok:
                minter_state = minter_msg
            elif self._last_minter_size is None:
                minter_state = "OK"
            else:
                minter_state = f"OK(cache={self._last_minter_size})"
            warm_ms = (time.monotonic() - t0) * 1000
            logger.info(
                f"[POT][Ready] 服务就绪 port={self._active_port} "
                f"pid={getattr(self._process, 'pid', '-')} token_len={self._last_token_len} "
                f"warm_ms={warm_ms:.0f} plugin={'OK' if plugin_ok else plugin_msg} "
                f"minter={minter_state} deno={'OK' if deno_ok else 'MISSING'}"
            )
            if not plugin_ok:
                logger.warning(
                    f"[POT][Ready] 警告 stage=plugin_missing {plugin_msg} "
                    "— 服务已就绪但 yt-dlp 无法加载插件，PO Token 不会被使用"
                )
            if not deno_ok:
                logger.warning(
                    "[POT][Ready] 警告 stage=deno_missing — 未找到 deno.exe，"
                    "POT 服务可能无法铸造 BotGuard Token。请在「设置 → 依赖组件」安装 JS Runtime (Deno)。"
                )
            self._warm_attempts = 0
        except Exception as e:
            self._schedule_warm_retry("error", detail=str(e))

    @staticmethod
    def _probe_deno() -> bool:
        """POT 服务端依赖 deno 铸 BotGuard Token；缺失时必须明确提示而非静默失败。"""
        from ..core.config_manager import config_manager
        from ..utils.paths import locate_runtime_tool

        configured = str(config_manager.get("js_runtime_path") or "").strip()
        if configured and Path(configured).exists():
            return True
        try:
            locate_runtime_tool("deno.exe", "js/deno.exe", "deno/deno.exe")
            return True
        except FileNotFoundError:
            return find_bundled_executable("deno.exe", "js/deno.exe", "deno/deno.exe") is not None
        except Exception:
            return False

    def _schedule_warm_retry(self, stage: str, detail: str = "") -> None:
        """记录失败原因并安排退避重试。"""
        delay = self._BACKOFF_SCHEDULE[min(self._warm_attempts, len(self._BACKOFF_SCHEDULE) - 1)]
        self._warm_attempts += 1
        self._warm_retry_at = time.monotonic() + delay
        logger.warning(
            f"[POT][Ready] 失败 stage={stage} {detail} 将在 {delay:.0f}s 后重试 "
            f"(第 {self._warm_attempts} 次)"
        )


# 单例实例
pot_manager = POTManager()
