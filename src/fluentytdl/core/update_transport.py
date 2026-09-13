"""Shared HTTPS transport and bounded update-source failover (no GUI dependencies)."""

from __future__ import annotations

import hashlib
import random
import ssl
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

import certifi
import requests
from requests.adapters import HTTPAdapter

CF_BASE = "https://fluentytdl.sakuraforgot.com/v1"
MANIFEST_URL = (
    "https://github.com/SakuraForgot/FluentYTDL/releases/latest/download/update-manifest.json"
)
REPOSITORIES = {
    "yt-dlp:stable": "yt-dlp/yt-dlp",
    "yt-dlp:nightly": "yt-dlp/yt-dlp-nightly-builds",
    "yt-dlp:master": "yt-dlp/yt-dlp-master-builds",
    "deno:stable": "denoland/deno",
    "ffmpeg:stable": "yt-dlp/FFmpeg-Builds",
    "pot-provider:stable": "jim60105/bgutil-ytdlp-pot-provider-rs",
    "atomicparsley:stable": "wez/atomicparsley",
}


def record(event: str, **fields) -> None:
    try:
        from ..observability.events import emit_event

        emit_event("signal", code="update_network", event=event, **fields)
    except Exception:
        pass


class UpdateError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, status: int = 0):
        self.code, self.retryable, self.status = code, retryable, status
        super().__init__(code)

    def __str__(self):
        from ..utils.control_center_text import text

        return text(self.code)


class SystemTLSAdapter(HTTPAdapter):
    """Use OS trust plus bundled public roots, including HTTPS proxy pools."""

    def __init__(self):
        self.context = ssl.create_default_context()
        self.context.load_verify_locations(cafile=certifi.where())
        super().__init__(max_retries=0)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self.context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        kwargs["ssl_context"] = self.context
        return super().proxy_manager_for(proxy, **kwargs)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host, pool = super().build_connection_pool_key_attributes(request, verify, cert)
        pool["ssl_context"] = self.context
        return host, pool


def make_session(proxy_mode="system", proxy_url="") -> requests.Session:
    session = requests.Session()
    session.mount("https://", SystemTLSAdapter())
    session.trust_env = proxy_mode == "system"
    session.headers.update({"User-Agent": "FluentYTDL/Update", "Accept-Encoding": "identity"})
    if proxy_mode in {"http", "socks5"} and proxy_url:
        scheme = "socks5h" if proxy_mode == "socks5" else "http"
        parsed = urlsplit(proxy_url if "://" in proxy_url else f"{scheme}://{proxy_url}")
        normalized = f"{scheme}://{parsed.netloc}"
        session.proxies = {"http": normalized, "https": normalized}
    return session


def classify(exc: Exception) -> UpdateError:
    if isinstance(exc, UpdateError):
        return exc
    if isinstance(exc, requests.exceptions.SSLError):
        detail = str(exc).lower()
        deterministic = any(
            s in detail
            for s in (
                "certificate_verify_failed",
                "certificate verify failed",
                "hostname",
                "certificate has expired",
            )
        )
        return UpdateError(
            "update_certificate" if deterministic else "update_tls", retryable=not deterministic
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return UpdateError("update_timeout", retryable=True)
    if isinstance(
        exc, (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError)
    ):
        return UpdateError("update_network", retryable=True)
    return UpdateError("update_invalid_response")


def check_response(response) -> None:
    if response.ok:
        return
    limited = response.status_code == 429 or (
        response.status_code == 403
        and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "rate limit" in response.text[:1000].lower()
        )
    )
    if limited:
        record(
            "rate_limited",
            status=response.status_code,
            remaining=response.headers.get("x-ratelimit-remaining"),
            reset=response.headers.get("x-ratelimit-reset"),
            retry_after=response.headers.get("retry-after"),
        )
        raise UpdateError("update_rate_limited", status=response.status_code)
    raise UpdateError(
        "update_service" if response.status_code >= 500 else "update_http",
        retryable=response.status_code >= 500,
        status=response.status_code,
    )


class Transport:
    def __init__(
        self, proxy_mode="system", proxy_url="", *, cancel=None, progress=None, session=None
    ):
        self.session = session or make_session(proxy_mode, proxy_url)
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda *_: None)
        self.proxy_mode = proxy_mode

    def retry(self, operation, **context):
        for attempt in range(1, 4):
            if self.cancel.is_set():
                raise UpdateError("update_cancelled")
            start = time.monotonic()
            try:
                result = operation()
                record(
                    "request_succeeded",
                    attempt=attempt,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    proxy_mode=self.proxy_mode,
                    **context,
                )
                return result
            except Exception as exc:
                error = classify(exc)
                record(
                    "request_failed",
                    attempt=attempt,
                    error_code=error.code,
                    status=error.status,
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    proxy_mode=self.proxy_mode,
                    **context,
                )
                if not error.retryable or attempt == 3:
                    raise error from exc
                self.progress(attempt + 1)
                if self.cancel.wait(2 ** (attempt - 1) + random.uniform(0, 0.2)):
                    raise UpdateError("update_cancelled") from exc
        raise AssertionError("unreachable")

    def get_json(self, url, **context):
        def operation():
            with self.session.get(url, timeout=(10, 15)) as response:
                check_response(response)
                data = response.json()
                if not isinstance(data, (dict, list)):
                    raise UpdateError("update_invalid_response")
                return data

        return self.retry(operation, host=urlsplit(url).hostname, **context)

    def get_text(self, url):
        def operation():
            with self.session.get(url, timeout=(10, 15)) as response:
                check_response(response)
                return response.text

        return self.retry(operation, host=urlsplit(url).hostname)

    def download(self, url, destination: Path, sha256="", progress=None):
        progress = progress or (lambda *_: None)

        def operation():
            digest = hashlib.sha256()
            downloaded = 0
            with self.session.get(url, timeout=(10, 45), stream=True) as response:
                check_response(response)
                size = int(response.headers.get("Content-Length") or 0)
                with destination.open("wb") as output:
                    for chunk in response.iter_content(65536):
                        if self.cancel.is_set():
                            raise UpdateError("update_cancelled")
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        progress(min(99, int(downloaded * 100 / size)) if size else 0)
                if size and size != downloaded:
                    raise UpdateError("update_truncated", retryable=True)
            if sha256 and digest.hexdigest().lower() != sha256.lower():
                raise UpdateError("update_hash")
            record(
                "download_verified",
                bytes=downloaded,
                sha256_checked=bool(sha256),
                host=urlsplit(url).hostname,
            )
            progress(100)
            return destination

        try:
            return self.retry(operation, host=urlsplit(url).hostname, stage="download")
        except Exception:
            destination.unlink(missing_ok=True)
            raise


class CheckSession:
    """One explicit check/batch, with at most one source transition."""

    def __init__(self, preferred="github", transport=None):
        self.preferred = "cloudflare" if preferred in {"cloudflare", "ghproxy"} else "github"
        self.source = self.preferred
        self.switched = False
        self.failed = False
        self.id = uuid.uuid4().hex
        self.transport = transport or Transport()
        self._lock = threading.RLock()

    def _request(self, github_url, cf_url, *, component, normalize):
        # Serialize batch requests to keep failover and the shared requests.Session consistent.
        with self._lock:
            if self.failed:
                raise UpdateError("update_sources_failed")
            while True:
                source = self.source
                try:
                    data = self.transport.get_json(
                        cf_url if source == "cloudflare" else github_url,
                        check_id=self.id,
                        preferred=self.preferred,
                        source=source,
                        component=component,
                    )
                    result = normalize(data, source)
                    record("check_succeeded", check_id=self.id, source=source, component=component)
                    return result
                except Exception as exc:
                    error = classify(exc)
                    can_switch = (
                        not self.switched
                        and (source == "cloudflare" or error.code == "update_rate_limited")
                        and error.code != "update_cancelled"
                    )
                    if not can_switch:
                        if self.switched:
                            self.failed = True
                        record(
                            "check_failed",
                            check_id=self.id,
                            source=source,
                            component=component,
                            error_code=error.code,
                        )
                        raise error from exc
                    self.source = "github" if source == "cloudflare" else "cloudflare"
                    self.switched = True
                    record(
                        "source_switched",
                        check_id=self.id,
                        preferred=self.preferred,
                        previous=source,
                        source=self.source,
                        reason=error.code,
                    )

    def release(self, component, channel="stable"):
        repo = REPOSITORIES[f"{component}:{channel}"]

        def normalize(data, source):
            if not isinstance(data.get("assets"), list) or not data.get("tag_name"):
                raise UpdateError("update_invalid_response")
            if source == "cloudflare":
                _fresh(data)
                if data.get("repository") != repo:
                    raise UpdateError("update_invalid_response")
            for asset in data["assets"]:
                name = asset.get("name", "")
                if not name or "/" in name or "\\" in name:
                    raise UpdateError("update_invalid_response")
                asset["browser_download_url"] = (
                    f"https://github.com/{repo}/releases/download/{quote(data['tag_name'], safe='')}/{quote(name, safe='')}"
                )
            return data

        return self._request(
            f"https://api.github.com/repos/{repo}/releases/latest",
            f"{CF_BASE}/updates/components/{component}?channel={channel}",
            component=component,
            normalize=normalize,
        )

    def manifest(self, channel="stable"):
        from ..utils.app_version import public_version, version_key

        if channel not in {"stable", "pre"}:
            raise ValueError("Unsupported update channel")

        def normalize(data, source):
            if source == "cloudflare":
                _fresh(data)
                if channel == "pre" and data.get("channel") != channel:
                    raise UpdateError("update_invalid_response")
                data = data.get("manifest", {})
            elif channel == "pre":
                if not isinstance(data, list):
                    raise UpdateError("update_invalid_response")
                candidates = [
                    r
                    for r in data
                    if not r.get("draft")
                    and public_version(r.get("tag_name", ""), "pre")
                    and any(a.get("name") == "update-manifest.json" for a in r.get("assets", []))
                ]
                if not candidates:
                    raise UpdateError("update_invalid_response")
                release = max(candidates, key=lambda r: version_key(r["tag_name"]))
                tag = release["tag_name"]
                data = self.transport.get_json(
                    f"https://github.com/SakuraForgot/FluentYTDL/releases/download/{quote(tag, safe='')}/update-manifest.json"
                )
                if data.get("release_tag") != tag or version_key(
                    data.get("app_version", "")
                ) != version_key(tag):
                    raise UpdateError("update_invalid_response")
                data["_is_prerelease"] = bool(release.get("prerelease"))
            if not data.get("app_version") or not isinstance(data.get("components"), dict):
                raise UpdateError("update_invalid_response")
            if not public_version(data["app_version"], channel):
                raise UpdateError("update_invalid_response")
            core = data["components"].get("app-core")
            if core:
                parsed = urlsplit(str(core.get("url", "")))
                name = parsed.path.rsplit("/", 1)[-1]
                if parsed.hostname != "github.com" or not name.endswith("-app-core.7z"):
                    raise UpdateError("update_invalid_response")
                tag = str(data.get("release_tag") or f"v{data['app_version']}")
                from urllib.parse import unquote

                core["url"] = (
                    f"https://github.com/SakuraForgot/FluentYTDL/releases/download/{quote(tag, safe='')}/{quote(unquote(name), safe='')}"
                )
            return data

        return self._request(
            MANIFEST_URL
            if channel == "stable"
            else "https://api.github.com/repos/SakuraForgot/FluentYTDL/releases?per_page=100",
            f"{CF_BASE}/updates/app?channel={channel}",
            component="app",
            normalize=normalize,
        )


def _fresh(data):
    from datetime import datetime

    try:
        stamp = datetime.fromisoformat(data["generated_at"].replace("Z", "+00:00")).timestamp()
        if not -300 < time.time() - stamp < 86400:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise UpdateError("update_cache_expired") from None


def configured_transport(**kwargs):
    from .config_manager import config_manager

    return Transport(
        str(config_manager.get("proxy_mode") or "system"),
        str(config_manager.get("proxy_url") or ""),
        **kwargs,
    )


def configured_check(**kwargs):
    from .config_manager import config_manager

    return CheckSession(
        str(config_manager.get("update_source") or "github"), configured_transport(**kwargs)
    )
