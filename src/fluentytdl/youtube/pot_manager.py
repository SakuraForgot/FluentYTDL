"""Owned original-bgutil lifecycle, bounded readiness and shared recovery."""

from __future__ import annotations

import atexit
import copy
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque

from loguru import logger
from PySide6.QtCore import QObject, Signal

from ..core.config_manager import config_manager
from ..core.pot_component import (
    ComponentError,
    resolve_bundle,
    runtime_compatible,
)
from ..core.runtime_activity import (
    active_count,
    changing_components,
    check_launch_cancelled,
    reading_components,
)
from ..utils.diagnostic_text import redact_detail
from ..utils.js_runtime import (
    clear_deno_selection,
    probe_deno,
    resolve_deno,
    select_verified_deno,
)
from ..utils.owned_process import close_job
from ..utils.paths import get_clean_env, user_data_dir
from ..utils.ui_text import tr_text


class POTSignals(QObject):
    state_changed = Signal(dict)
    retry_requested = Signal()
    check_requested = Signal()
    enabled_requested = Signal(bool)
    component_changed = Signal(str)


def effective_proxy():
    mode = str(config_manager.get("proxy_mode") or "off").lower()
    value = str(config_manager.get("proxy_url") or "").strip()
    if mode == "system":
        proxies = urllib.request.getproxies()
        return str(proxies.get("https") or proxies.get("http") or proxies.get("all") or "")
    if mode in {"http", "socks5"} and value:
        return value if "://" in value else f"{'socks5' if mode == 'socks5' else 'http'}://{value}"
    return ""


class POTManager:
    _instance = None
    DEFAULT_PORT = 4416
    PORT_RANGE = 10
    _BACKOFF_SCHEDULE = (30.0, 120.0, 300.0)
    _NON_RETRYABLE = {
        "pot_package_missing",
        "pot_package_corrupt",
        "pot_package_incomplete",
        "pot_package_incompatible",
        "pot_runtime_missing",
        "pot_runtime_invalid",
        "pot_runtime_incompatible",
        "pot_plugin_conflict",
        "pot_permission_denied",
    }
    _NON_RETRYABLE |= {
        "pot_app_incompatible",
        "pot_plugin_incompatible",
        "pot_package_unlisted",
        "pot_package_manifest",
        "pot_activation_invalid",
        "pot_ytdlp_missing",
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self._lock = threading.RLock()
        self._process = self._probe_process = self._warm_thread = self._retry_timer = None
        self._active_port = self._warm_attempts = self._last_token_len = self._generation = 0
        self._warm_retry_at = 0.0
        self._is_running = self._shutdown = self._restart_requested = self._incident_active = False
        self._check_requested = False
        self._warm_event, self._cancel = threading.Event(), threading.Event()
        self._last_minter_size = self._bundle = self._runtime = self._job_handle = None
        self._history = deque(maxlen=100)
        self._server_tail = deque(maxlen=8)
        self._state = dict(
            state="stopped",
            phase="",
            code="",
            detail="",
            generation=0,
            incident=0,
            checked_at=0,
            retry_at=0,
            components={},
        )
        self.signals = POTSignals()
        self.signals.retry_requested.connect(self.request_retry)
        self.signals.check_requested.connect(self.request_check)
        self.signals.enabled_requested.connect(self.set_enabled)
        self.signals.component_changed.connect(self._component_changed)
        config_manager.configChanged.connect(self._config_changed)
        if sys.platform == "win32":
            self._setup_job_object()
        atexit.register(self.stop_server)

    def _setup_job_object(self):
        try:
            import win32job

            handle = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation, info
            )
            self._job_handle = handle
        except Exception as error:
            self._job_handle = None
            logger.warning("POT job unavailable: {}", type(error).__name__)

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            result.update(
                enabled=bool(config_manager.get("pot_provider_enabled", False)),
                running=self.is_running(),
                port=self._active_port,
                pid=getattr(self._process, "pid", None),
                token_length=self._last_token_len,
                history=list(self._history),
            )
            return result

    def _publish(
        self,
        state,
        *,
        phase="",
        code="",
        detail="",
        generation=None,
        expected_process=None,
        **fields,
    ):
        with self._lock:
            if generation is not None and generation != self._generation:
                return
            if expected_process is not None and (
                self._process is not expected_process
                or (state == "ready" and not self.is_running())
            ):
                return
            if state == "unavailable" and not self._incident_active:
                self._state["incident"] += 1
                self._incident_active = True
            if state in {"ready", "stopped"}:
                self._incident_active = False
            self._state.update(
                state=state,
                phase=phase,
                code=code,
                detail=redact_detail(detail),
                generation=self._generation,
                **fields,
            )
            if state in {"ready", "unavailable", "stopped"}:
                self._history.append(
                    dict(
                        time=time.time(),
                        state=state,
                        phase=phase,
                        code=code,
                        detail=redact_detail(detail),
                    )
                )
        try:
            self.signals.state_changed.emit(self.snapshot())
        except RuntimeError:
            pass  # QApplication may already have destroyed its signal bridge at exit.

    def set_enabled(self, enabled):
        enabled = bool(enabled)
        if enabled != bool(config_manager.get("pot_provider_enabled", False)):
            config_manager.set("pot_provider_enabled", enabled)

    def _config_changed(self, key, _value):
        if key in {
            "pot_provider_enabled",
            "proxy_mode",
            "proxy_url",
            "js_runtime",
            "js_runtime_path",
            "yt_dlp_exe_path",
            "ytdlp_channel",
        }:
            self._invalidate()

    def _component_changed(self, key):
        if key in {"pot-provider", "deno", "yt-dlp"}:
            self._invalidate()

    def _invalidate(self):
        clear_deno_selection()
        with self._lock:
            self._warm_event.clear()
            self._generation += 1
            self._cancel.set()
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            self._state["retry_at"] = self._warm_retry_at = 0
            self._restart_requested = True
            running = self._warm_thread and self._warm_thread.is_alive()
            if not config_manager.get("pot_provider_enabled", False) and (running or self._process):
                self._publish("stopping")
        if not running:
            self.ensure_warm_async(force=True)

    def request_retry(self):
        self.ensure_warm_async(force=True)

    def request_check(self):
        self.ensure_warm_async(force=True, restart=False)

    def ensure_warm_async(self, *, force=False, restart=True):
        with self._lock:
            if self._shutdown:
                return
            if self._warm_thread and self._warm_thread.is_alive():
                if force:
                    # Terminal signals can reach the UI before worker cleanup.
                    # Retain explicit actions; a restart takes priority over a check.
                    self._restart_requested |= restart
                    self._check_requested |= not restart
                return
            if not config_manager.get("pot_provider_enabled", False) and self._process is None:
                self._publish("stopped")
                return
            if not force and (self.is_warm or time.monotonic() < self._warm_retry_at):
                return
            if self._retry_timer:
                self._retry_timer.cancel()
                self._retry_timer = None
            if force:
                self._warm_attempts = 0
            self._cancel = threading.Event()
            self._restart_requested = False
            self._check_requested = False
            self._generation += 1
            if restart or not self.is_warm:
                self._warm_event.clear()
                self._last_token_len = 0
                self._state["components"] = {}
            self._state["retry_at"] = self._warm_retry_at = 0
            self._warm_thread = threading.Thread(
                target=self._warm_worker,
                args=(self._generation, self._cancel, restart),
                name="pot-readiness",
                daemon=True,
            )
            self._warm_thread.start()

    def _check_current(self, generation, cancel):
        if cancel.is_set() or generation != self._generation:
            raise InterruptedError("pot_check_cancelled")

    def _choose_runtime(self, bundle):
        chosen = resolve_deno()
        try:
            runtime = probe_deno(chosen, timeout=5)
            if runtime_compatible(bundle, runtime.version):
                return runtime
            error = ComponentError("pot_runtime_incompatible")
        except (OSError, ValueError, subprocess.TimeoutExpired) as failure:
            error = ComponentError(
                "pot_runtime_missing"
                if isinstance(failure, FileNotFoundError)
                else "pot_runtime_invalid"
            )
        raise error

    def _warm_worker(self, generation=None, cancel=None, restart=True):
        generation = self._generation if generation is None else generation
        cancel = self._cancel if cancel is None else cancel
        with self._lock:
            checked_process = (
                self._process if not restart and self.is_running() and self._bundle else None
            )
        read_only = checked_process is not None
        try:
            self._publish(
                "waiting"
                if not read_only and active_count()
                else "checking"
                if config_manager.get("pot_provider_enabled", False)
                else "stopping",
                phase="components",
                generation=generation,
            )
            guard = reading_components if read_only else changing_components
            with guard(cancel):
                self._check_current(generation, cancel)
                if not config_manager.get("pot_provider_enabled", False):
                    if read_only:
                        # A settings change can arrive just after _check_current.
                        # Only the queued exclusive operation may stop the server.
                        with self._lock:
                            self._restart_requested = True
                        raise InterruptedError("pot_check_cancelled")
                    self._stop_owned()
                    self._publish("stopped", generation=generation, components={})
                    return
                deadline = time.monotonic() + 45
                self._publish("checking", phase="components", generation=generation, components={})
                bundle = resolve_bundle(verify=True)
                runtime = self._choose_runtime(bundle)
                self._check_current(generation, cancel)
                select_verified_deno(runtime, config_manager)
                if read_only and (self._process is not checked_process or not self.is_running()):
                    raise ComponentError("pot_process_exited")
                if read_only and self._bundle.root != bundle.root:
                    raise ComponentError("pot_activation_invalid")
                if not read_only and (
                    restart or self._bundle is None or self._bundle.root != bundle.root
                ):
                    self._stop_owned()
                self._bundle, self._runtime = bundle, runtime
                self._publish(
                    "starting",
                    phase="server",
                    generation=generation,
                    components=dict(
                        deno="passed", server="checking", plugin="unchecked", token="unchecked"
                    ),
                    bundle_id=bundle.bundle_id,
                    version=bundle.version,
                    bundle_path=str(bundle.root),
                    deno_path=str(runtime.path),
                    deno_version=runtime.version,
                    deno_source=runtime.source,
                )
                if read_only:
                    self._ping_owned(deadline)
                else:
                    self._start_owned(deadline, cancel)
                    checked_process = self._process
                self._check_current(generation, cancel)
                self._publish(
                    "probing",
                    phase="plugin",
                    generation=generation,
                    components=dict(
                        deno="passed", server="passed", plugin="checking", token="unchecked"
                    ),
                )

                def plugin_loaded():
                    self._check_current(generation, cancel)
                    self._publish(
                        "probing",
                        phase="token",
                        generation=generation,
                        components=dict(
                            deno="passed", server="passed", plugin="passed", token="checking"
                        ),
                    )

                ok, detail = self.probe_ytdlp_provider(
                    timeout=min(20, max(0.1, deadline - time.monotonic())),
                    on_plugin=plugin_loaded,
                )
                with self._lock:
                    self._check_current(generation, cancel)
                    if not ok:
                        raise ComponentError(
                            detail if detail.startswith("pot_") else "pot_cli_probe_failed"
                        )
                    if not self.is_running() or self._process is not checked_process:
                        raise ComponentError("pot_process_exited")
                    self._warm_attempts = 0
                    self._warm_event.set()
                self._publish(
                    "ready",
                    phase="complete",
                    generation=generation,
                    expected_process=checked_process,
                    checked_at=time.time(),
                    components=dict(
                        deno="passed", server="passed", plugin="passed", token="passed"
                    ),
                )
        except InterruptedError:
            pass
        except Exception as error:
            if generation == self._generation and not cancel.is_set():
                code = getattr(error, "code", "") or (
                    "pot_runtime_missing"
                    if isinstance(error, FileNotFoundError)
                    else "pot_permission_denied"
                    if isinstance(error, PermissionError)
                    else "pot_timeout"
                    if isinstance(error, subprocess.TimeoutExpired)
                    else "pot_start_failed"
                )
                self._warm_event.clear()
                self._last_token_len = 0
                components = {
                    key: "failed" if state == "checking" else state
                    for key, state in self._state.get("components", {}).items()
                }
                if code.startswith("pot_runtime_"):
                    components["deno"] = "failed"
                if components.get("server") == "passed" and not self.is_running():
                    components["server"] = "failed"
                if code == "pot_process_exited":
                    components.update(server="failed", token="unchecked")
                    self._last_token_len = 0
                self._publish(
                    "unavailable",
                    phase=self._state.get("phase", ""),
                    code=code,
                    detail=str(error),
                    generation=generation,
                    components=components,
                    checked_at=time.time(),
                )
                from ..observability import emit_event

                emit_event(
                    "signal", stage="startup", subsystem="pot", code=code, generation=generation
                )
                self._schedule_warm_retry(code)
        finally:
            with self._lock:
                self._warm_thread = None
                if not self._shutdown and (self._restart_requested or self._check_requested):
                    self.ensure_warm_async(force=True, restart=self._restart_requested)

    def _find_available_port(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _local_urlopen(request, timeout=5):
        return urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=timeout
        )

    def _environment(self):
        env = get_clean_env()
        for key in list(env):
            if key.lower().endswith("_proxy"):
                env.pop(key)
        proxy = effective_proxy()
        if proxy:
            env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy)
        cache = user_data_dir() / "cache/pot-deno"
        cache.mkdir(parents=True, exist_ok=True)
        env.update(
            DENO_DIR=str(cache), DENO_NO_UPDATE_CHECK="1", NO_PROXY="127.0.0.1,localhost,::1"
        )
        return env

    def _ping_owned(self, deadline):
        # A live process alone does not prove that the service still responds.
        try:
            with self._local_urlopen(
                f"http://127.0.0.1:{self._active_port}/ping",
                timeout=min(2, max(0.1, deadline - time.monotonic())),
            ) as reply:
                if json.load(reply).get("version") == self._bundle.version:
                    return
        except (OSError, ValueError):
            pass
        raise ComponentError("pot_local_health_failed")

    def _start_owned(self, deadline, cancel):
        if self.is_running():
            self._ping_owned(deadline)
            return
        for _attempt in range(3):
            self._server_tail.clear()
            self._active_port = self._find_available_port()
            process = subprocess.Popen(
                self._bundle.command(self._runtime.path, self._active_port),
                cwd=self._bundle.cwd,
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            with self._lock:
                self._process = process
            if self._job_handle:
                try:
                    import win32api
                    import win32con
                    import win32job

                    handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, process.pid)
                    try:
                        win32job.AssignProcessToJobObject(self._job_handle, handle)
                    finally:
                        handle.Close()
                except Exception as error:
                    logger.warning("POT job assignment failed: {}", type(error).__name__)
            threading.Thread(
                target=self._read_server, args=(process,), daemon=True, name="pot-output"
            ).start()
            end = min(deadline, time.monotonic() + 10)
            while time.monotonic() < end and process.poll() is None and not cancel.is_set():
                try:
                    with self._local_urlopen(
                        f"http://127.0.0.1:{self._active_port}/ping", timeout=0.5
                    ) as reply:
                        if json.load(reply).get("version") == self._bundle.version:
                            with self._lock:
                                if cancel.is_set() or self._process is not process:
                                    raise InterruptedError("pot_check_cancelled")
                                self._is_running = True
                            return
                except (OSError, ValueError):
                    pass
                cancel.wait(0.1)
            self._stop_owned()
            if cancel.is_set():
                raise InterruptedError()
            if time.monotonic() >= deadline:
                break
        raise ComponentError("pot_local_start_failed", "\n".join(self._server_tail))

    def _read_server(self, process):
        try:
            for raw in process.stdout:
                line = redact_detail(raw.decode("utf-8", "replace").strip())
                if self._process is process and (
                    "error" in line.lower() or "failed" in line.lower()
                ):
                    self._server_tail.append(line)
                if "error" in line.lower() or "failed" in line.lower():
                    logger.warning("POT server: {}", line)
            process.wait()
        finally:
            with self._lock:
                unexpected_exit = (
                    self._process is process and self._is_running and not self._shutdown
                )
                if unexpected_exit:
                    self._warm_event.clear()
                    self._is_running = False
                    self._last_token_len = 0
                    components = {
                        **self._state.get("components", {}),
                        "server": "failed",
                        "plugin": "unchecked",
                        "token": "unchecked",
                    }
            if unexpected_exit:
                self._publish(
                    "unavailable",
                    phase="server",
                    code="pot_process_exited",
                    detail=f"exit={process.returncode}",
                    expected_process=process,
                    components=components,
                )
                with self._lock:
                    if self._process is process:
                        self._schedule_warm_retry("pot_process_exited")

    @staticmethod
    def _terminate(process):
        if process is None:
            return
        close_job(process)
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _stop_owned(self):
        with self._lock:
            self._warm_event.clear()
            self._is_running = False
            self._last_token_len = 0
            process, self._process = self._process, None
            self._active_port = 0
        self._terminate(process)

    def stop_server(self):
        """Shutdown only; disabling through the UI waits for active operations."""
        self._shutdown = True
        self._generation += 1
        self._cancel.set()
        if self._retry_timer:
            self._retry_timer.cancel()
        self._terminate(self._probe_process)
        self._stop_owned()
        worker = self._warm_thread
        if worker and worker is not threading.current_thread():
            worker.join(timeout=3)
        if self._job_handle:
            self._job_handle.Close()
            self._job_handle = None
        self._publish("stopped", components={})

    def is_running(self):
        return bool(self._is_running and self._process and self._process.poll() is None)

    @property
    def is_warm(self):
        return self._warm_event.is_set() and self.is_running()

    @property
    def active_port(self):
        return self._active_port

    @property
    def last_minter_size(self):
        return None

    def get_extractor_args(self):
        return (
            f"youtubepot-bgutilhttp:base_url=http://127.0.0.1:{self._active_port}"
            if self.is_running()
            else None
        )

    def wait_until_ready(self, timeout=45.0, *, cancel_check=None):
        check_launch_cancelled(cancel_check)
        if self.is_warm:
            return True
        self.ensure_warm_async()
        with self._lock:
            worker = self._warm_thread
        if worker and worker is not threading.current_thread():
            if cancel_check is None:
                worker.join(timeout=max(0, timeout))
            else:
                deadline = time.monotonic() + max(0, timeout)
                while worker.is_alive() and not self.is_warm:
                    check_launch_cancelled(cancel_check)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    worker.join(timeout=min(0.1, remaining))
        check_launch_cancelled(cancel_check)
        return self.is_warm

    def _schedule_warm_retry(self, stage, detail=""):
        with self._lock:
            if self._retry_timer:
                self._retry_timer.cancel()
            if (
                self._shutdown
                or not config_manager.get("pot_provider_enabled", False)
                or stage in self._NON_RETRYABLE
                or self._warm_attempts >= len(self._BACKOFF_SCHEDULE)
            ):
                self._warm_retry_at = 0
                self._state["retry_at"] = 0
                return
            delay = self._BACKOFF_SCHEDULE[self._warm_attempts]
            self._warm_attempts += 1
            self._warm_retry_at = time.monotonic() + delay
            self._state["retry_at"] = time.time() + delay
            generation = self._generation

            def retry():
                if generation == self._generation and not self._shutdown:
                    self.ensure_warm_async()

            self._retry_timer = threading.Timer(delay, retry)
            self._retry_timer.daemon = True
            self._retry_timer.start()
        try:
            self.signals.state_changed.emit(self.snapshot())
        except RuntimeError:
            pass

    def verify_plugin_loadable(self):
        from .yt_dlp_cli import pot_plugin_directory

        try:
            path = pot_plugin_directory()
            return (True, str(path)) if path else (False, "pot_plugin_missing")
        except Exception as error:
            return False, redact_detail(error)

    def probe_ytdlp_provider(self, url=None, timeout=20.0, *, on_plugin=None):
        from .pot_probe import probe_provider
        from .yt_dlp_cli import pot_plugin_directory, resolve_yt_dlp_exe

        exe = resolve_yt_dlp_exe()
        if not exe:
            return False, "pot_ytdlp_missing"
        try:
            plugin = pot_plugin_directory(exe)
            if not plugin:
                return False, "pot_plugin_missing"
            self._last_token_len = probe_provider(
                exe,
                plugin,
                f"http://127.0.0.1:{self._active_port}",
                effective_proxy(),
                timeout=timeout,
                env=self._environment(),
                on_process=lambda p: setattr(self, "_probe_process", p),
                on_plugin=on_plugin,
            )
            return True, f"Token length={self._last_token_len}"
        except Exception as error:
            if isinstance(error, ComponentError):
                raise
            return False, "pot_timeout" if isinstance(error, subprocess.TimeoutExpired) else str(
                error
            )

    def try_recover(self):
        self.request_retry()
        return self.wait_until_ready()

    def verify_token_generation(self, timeout=20.0):
        return self.probe_ytdlp_provider(timeout=timeout)

    def get_health_status(self):
        return {
            **self.snapshot(),
            "running": self.is_running(),
            "overall_ok": self.is_warm,
            "token_ok": self.is_warm,
            "token_len": self._last_token_len,
            "summary": self.status_brief(),
        }

    @staticmethod
    def _probe_deno():
        try:
            probe_deno(resolve_deno())
            return True
        except Exception:
            return False

    def status_brief(self, state=None):
        return {
            "stopped": tr_text("已关闭"),
            "stopping": tr_text("正在停止"),
            "checking": tr_text("正在检查组件"),
            "waiting": tr_text("等待当前任务结束"),
            "starting": tr_text("正在加载"),
            "probing": tr_text("正在验证连接"),
            "ready": tr_text("已就绪"),
            "unavailable": tr_text("未就绪"),
        }.get((self._state if state is None else state)["state"], tr_text("未检查"))


pot_manager = POTManager()
