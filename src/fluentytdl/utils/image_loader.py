from __future__ import annotations

import io
import sys
from functools import lru_cache

from PIL import Image
from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkDiskCache,
    QNetworkProxy,
    QNetworkProxyFactory,
    QNetworkReply,
    QNetworkRequest,
    QSsl,
    QSslCertificate,
    QSslConfiguration,
    QSslSocket,
)

from fluentytdl.utils.localized_log import log_text

from ..core.config_manager import config_manager
from .logger import logger
from .tls_trust import _tls_context

# 全局共享的 QNetworkAccessManager（复用 HTTP 连接）
_global_manager: QNetworkAccessManager | None = None
_global_manager_initialized: bool = False
# P4: 全局单例 ImageLoader，避免每个窗口重复创建 Signal
_global_image_loader: ImageLoader | None = None

MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
IMAGE_TIMEOUT_MS = 30_000
SAFE_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP")


@lru_cache(maxsize=1)
def _thumbnail_tls() -> QSslConfiguration:
    # Use Windows native TLS. The frozen Windows build excludes the unused
    # OpenSSL Qt plugin so it cannot probe incompatible DLLs from PATH.
    if sys.platform == "win32" and not QSslSocket.setActiveBackend("schannel"):
        raise RuntimeError("Windows TLS backend unavailable")
    config = QSslConfiguration.defaultConfiguration()
    config.setCaCertificates(
        [
            QSslCertificate(root, QSsl.EncodingFormat.Der)
            for root in _tls_context().get_ca_certs(binary_form=True)
        ]
    )
    return config


def decode_thumbnail(data: bytes) -> QPixmap:
    """Select only raster decoders; reject oversized images before allocation."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Thumbnail byte limit exceeded")
    with Image.open(io.BytesIO(data), formats=SAFE_IMAGE_FORMATS) as image:
        if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_IMAGE_PIXELS:
            raise ValueError("Thumbnail pixel limit exceeded")
        rgba = image.convert("RGBA")
        pixels = rgba.tobytes("raw", "RGBA")
        result = QImage(pixels, rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
        return QPixmap.fromImage(result.copy())


def get_image_loader() -> ImageLoader:
    """获取全局单例 ImageLoader（P4）"""
    global _global_image_loader
    if _global_image_loader is None:
        _global_image_loader = ImageLoader()
    return _global_image_loader


def trim_disk_cache(max_mb: int = 50) -> None:
    """清理磁盘缓存，删除最旧的文件直到总大小低于 max_mb（P5）"""
    import tempfile
    from pathlib import Path as _Path

    cache_dir = _Path(tempfile.gettempdir()) / "fluentytdl_thumb_cache"
    if not cache_dir.exists():
        return
    try:
        files = sorted(
            cache_dir.iterdir(),
            key=lambda f: f.stat().st_atime if f.is_file() else float("inf"),
        )
        total = sum(f.stat().st_size for f in files if f.is_file())
        max_bytes = max_mb * 1024 * 1024
        removed = 0
        for f in files:
            if total <= max_bytes:
                break
            if f.is_file():
                size = f.stat().st_size
                try:
                    f.unlink()
                    total -= size
                    removed += 1
                except Exception:
                    pass
        if removed:
            log_text(
                logger,
                "info",
                "[ImageLoader] 磁盘缓存清理：删除 {0} 个旧文件，当前 {1} MB",
                removed,
                total // 1024 // 1024,
            )
    except Exception as e:
        log_text(logger, "warning", "[ImageLoader] 磁盘缓存清理失败: {0}", e)


def _get_global_manager() -> QNetworkAccessManager:
    """获取全局共享的网络管理器，复用 HTTP 连接"""
    global _global_manager, _global_manager_initialized

    if _global_manager is None:
        _global_manager = QNetworkAccessManager()

        # 启用磁盘缓存
        try:
            import tempfile
            from pathlib import Path

            cache_dir = Path(tempfile.gettempdir()) / "fluentytdl_thumb_cache"
            cache_dir.mkdir(parents=True, exist_ok=True)

            # P5：初始化时裁剪超限缓存
            trim_disk_cache(max_mb=50)

            disk_cache = QNetworkDiskCache()
            disk_cache.setCacheDirectory(str(cache_dir))
            disk_cache.setMaximumCacheSize(50 * 1024 * 1024)  # 50MB 缓存
            _global_manager.setCache(disk_cache)
            log_text(logger, "debug", "[ImageLoader] 已启用磁盘缓存: {}", cache_dir)
        except Exception as e:
            log_text(logger, "warning", "[ImageLoader] 磁盘缓存初始化失败: {}", e)

    if not _global_manager_initialized:
        _global_manager_initialized = True
        _apply_proxy_to_manager(_global_manager)

    return _global_manager


def _apply_proxy_to_manager(manager: QNetworkAccessManager) -> None:
    """应用代理配置到网络管理器"""
    proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
    proxy_url_str = str(config_manager.get("proxy_url", "") or "").strip()

    if proxy_mode == "off":
        try:
            QNetworkProxyFactory.setUseSystemConfiguration(False)
        except Exception:
            pass
        manager.setProxy(QNetworkProxy(QNetworkProxy.ProxyType.NoProxy))
        return

    if proxy_mode == "system":
        try:
            QNetworkProxyFactory.setUseSystemConfiguration(True)
        except Exception:
            pass
        return

    if not proxy_url_str:
        log_text(logger, "warning", "[ImageLoader] 代理模式=手动，但 URL 为空")
        return

    lower = proxy_url_str.lower()
    if not (
        lower.startswith("http://") or lower.startswith("https://") or lower.startswith("socks5://")
    ):
        scheme = "socks5" if proxy_mode == "socks5" else "http"
        proxy_url_str = f"{scheme}://" + proxy_url_str
        lower = proxy_url_str.lower()

    url = QUrl(proxy_url_str)
    if not url.isValid() or not url.host() or url.port() <= 0:
        log_text(logger, "error", "[ImageLoader] 代理 URL 无效: {}", proxy_url_str)
        return

    proxy_type = QNetworkProxy.ProxyType.HttpProxy
    if proxy_mode == "socks5" or lower.startswith("socks5://") or "socks5" in lower:
        proxy_type = QNetworkProxy.ProxyType.Socks5Proxy

    proxy = QNetworkProxy(proxy_type, url.host(), url.port())
    manager.setProxy(proxy)
    log_text(logger, "info", "[ImageLoader] 配置代理: {}:{}", url.host(), url.port())


class ImageLoader(QObject):
    """通用异步图片加载器

    优化特性：
    - 全局共享 QNetworkAccessManager，复用 HTTP 连接
    - 50MB 磁盘缓存，加速重复加载
    - 支持 HTTP Keep-Alive
    - WebP→JPG 自动转换（兼容性）
    """

    loaded = Signal(QPixmap)
    loaded_with_url = Signal(str, QPixmap)
    failed = Signal(str)  # 加载失败时发射，参数为 URL

    def __init__(self, parent=None):
        super().__init__(parent)
        # 使用全局共享的网络管理器
        self.manager = _get_global_manager()

    def load(
        self,
        url_str: str,
        target_size: tuple[int, int] | None = None,
        radius: int = 0,
        allow_webp: bool = False,
    ) -> None:
        """开始加载图片。"""
        original_req_url = str(url_str or "").strip()
        url_str = original_req_url
        if not url_str:
            return

        if not allow_webp:
            url_str = self._force_youtube_webp_to_jpg(url_str)

        url = QUrl(url_str)
        if not url.isValid() or url.scheme().lower() not in {"http", "https"} or not url.host():
            self.failed.emit(original_req_url)
            return
        request = QNetworkRequest(url)
        request.setSslConfiguration(_thumbnail_tls())
        request.setTransferTimeout(IMAGE_TIMEOUT_MS)
        request.setAttribute(QNetworkRequest.Attribute.Http2AllowedAttribute, False)
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )

        # 设置请求头
        request.setRawHeader(
            b"User-Agent",
            b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        )

        # 启用缓存
        request.setAttribute(
            QNetworkRequest.Attribute.CacheLoadControlAttribute,
            QNetworkRequest.CacheLoadControl.PreferCache,
        )

        # 启用 HTTP 流水线（如果服务器支持）
        request.setAttribute(
            QNetworkRequest.Attribute.HttpPipeliningAllowedAttribute,
            True,
        )

        reply = self.manager.get(request)
        reply.setReadBufferSize(64 * 1024)
        payload = bytearray()
        timer = QTimer(reply)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(IMAGE_TIMEOUT_MS)

        def receive():
            available = reply.bytesAvailable()
            if len(payload) + available > MAX_IMAGE_BYTES:
                reply.abort()
                return
            payload.extend(bytes(reply.read(available)))

        def check_length():
            length = reply.header(QNetworkRequest.KnownHeaders.ContentLengthHeader)
            if length is not None and int(length) > MAX_IMAGE_BYTES:
                reply.abort()

        reply.readyRead.connect(receive)
        reply.metaDataChanged.connect(check_length)
        reply.finished.connect(timer.stop)

        # SSL 错误处理
        try:
            reply.sslErrors.connect(
                lambda errors: log_text(
                    logger,
                    "warning",
                    "[ImageLoader] SSL 警告 ({}): {}",
                    url_str,
                    ", ".join(e.errorString() for e in errors),
                )
            )
        except Exception:
            pass

        reply.finished.connect(
            lambda: self._on_finished(reply, target_size, radius, original_req_url, payload)
        )

    def _on_finished(
        self,
        reply,
        target_size: tuple[int, int] | None,
        radius: int,
        original_url: str,
        payload: bytearray,
    ) -> None:
        try:
            # 如果绑定的 UI 控件已经被销毁，C++ 对象也会被销毁，这里会引发 RuntimeError
            self.objectName()
        except RuntimeError:
            try:
                reply.deleteLater()
            except Exception:
                pass
            return

        try:
            # 1. 检查网络错误
            err = reply.error()
            if err != QNetworkReply.NetworkError.NoError:
                self.failed.emit(str(original_url))
                return

            # 2. 检查数据有效性
            if len(payload) + reply.bytesAvailable() > MAX_IMAGE_BYTES:
                self.failed.emit(str(original_url))
                return
            payload.extend(bytes(reply.readAll()))
            try:
                pixmap = decode_thumbnail(bytes(payload))
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                log_text(logger, "warning", "[ImageLoader] Pillow 解码失败: {0}", error)
                self.failed.emit(str(original_url))
                return

            # 4. 后处理 (缩放/圆角)
            if target_size:
                w, h = target_size
                pixmap = pixmap.scaled(
                    w,
                    h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )

            if radius > 0:
                pixmap = self._round_corners(pixmap, radius)

            self.loaded.emit(pixmap)
            self.loaded_with_url.emit(str(original_url), pixmap)
        except RuntimeError:
            # 额外防护：在处理过程中如果对象被销毁
            pass
        finally:
            try:
                reply.deleteLater()
            except Exception:
                pass

    @staticmethod
    def _round_corners(source: QPixmap, radius: int) -> QPixmap:
        if source.isNull():
            return source
        target = QPixmap(source.size())
        target.fill(Qt.GlobalColor.transparent)
        painter = QPainter(target)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, source.width(), source.height(), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, source)
        painter.end()
        return target

    @staticmethod
    def _force_youtube_webp_to_jpg(url_str: str) -> str:
        """将 YouTube WebP 缩略图 URL 转换为 JPG 格式"""
        # YouTube 缩略图常见：
        # https://i.ytimg.com/vi_webp/VIDEO_ID/maxresdefault.webp
        # -> https://i.ytimg.com/vi/VIDEO_ID/maxresdefault.jpg
        if "/vi_webp/" in url_str:
            url_str = url_str.replace("/vi_webp/", "/vi/")
        if url_str.endswith(".webp"):
            url_str = url_str[:-5] + ".jpg"
        return url_str
