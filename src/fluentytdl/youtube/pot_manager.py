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
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from loguru import logger

from ..utils.paths import find_bundled_executable, frozen_app_dir


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

        # 注册退出清理
        atexit.register(self.stop_server)

        # Windows: 设置 Job Object
        if sys.platform == "win32":
            self._setup_job_object()

    def _setup_job_object(self):
        """创建 Windows Job Object，确保子进程随父进程终止"""
        try:
            import win32job

            job_handle = win32job.CreateJobObject(None, "FluentYTDL_POT_Job")
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
                env = os.environ.copy()
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
                        # 进程还在运行，可能只是健康检查未通过
                        logger.warning("POT Manager: 健康检查未通过，但进程仍在运行，标记为已启动")
                        self._is_running = True
                        return True
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
                if cache_size is not None:
                    return True, f"Minter 缓存正常 (条目: {cache_size})"
                return True, "Minter 已初始化"
            elif isinstance(data, list):
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

        # L1: Token 生成能力
        token_ok, token_detail = self.verify_token_generation()
        result["token_ok"] = token_ok
        result["token_detail"] = token_detail

        # 综合判定
        result["overall_ok"] = result["running"] and token_ok
        if result["overall_ok"]:
            result["summary"] = f"运行中 (端口 {self._active_port}), {token_detail}"
        elif result["running"] and not token_ok:
            result["summary"] = f"运行中但 Token 生成异常: {token_detail}"
        else:
            result["summary"] = f"异常: {token_detail}"

        return result

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

    def wait_until_ready(self, timeout: float = 15.0) -> bool:
        """等待 POT 服务预热完成

        如果已预热 → 立即返回 True
        如果未预热 → 等待 _warm_event 信号，最多等 timeout 秒
        如果等待超时且服务在运行 → 尝试主动预热

        Args:
            timeout: 最大等待时间（秒）

        Returns:
            True 如果 POT 已就绪可用
        """
        if self._warm_event.is_set():
            return True

        if not self.is_running():
            return False

        # 等待后台预热完成
        logger.debug(f"POT Manager: 等待预热完成 (最多 {timeout}s)...")
        ready = self._warm_event.wait(timeout=timeout)

        if ready:
            return True

        # 超时后仍未就绪 → 主动尝试一次
        logger.info("POT Manager: 预热等待超时，主动尝试生成 Token...")
        ok, msg = self.verify_token_generation(timeout=20.0)
        if ok:
            logger.info(f"POT Manager: 主动预热成功: {msg}")
        else:
            logger.warning(f"POT Manager: 主动预热失败: {msg}")
        return ok


# 单例实例
pot_manager = POTManager()
