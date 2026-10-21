"""Exercise the untrusted image boundary without loading unsafe decoder plugins."""

import io
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from PIL import Image
from PySide6.QtWidgets import QApplication

from fluentytdl.utils import image_loader


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "GIF", "BMP"])
def test_common_thumbnails_still_decode(format):
    data = io.BytesIO()
    Image.new("RGB", (12, 9), "red").save(data, format=format)
    result = image_loader.decode_thumbnail(data.getvalue())
    assert (result.width(), result.height()) == (12, 9)


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>',
        b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 1 1\n",
        b"not an image",
    ],
)
def test_non_raster_formats_rejected(data):
    with pytest.raises((OSError, ValueError)):
        image_loader.decode_thumbnail(data)


def test_size_checked_before_pixel_conversion(monkeypatch):
    data = io.BytesIO()
    Image.new("RGB", (20, 20)).save(data, format="PNG")
    monkeypatch.setattr(image_loader, "MAX_IMAGE_PIXELS", 100)
    monkeypatch.setattr(Image.Image, "convert", lambda *_: pytest.fail("decoded oversized image"))
    with pytest.raises(ValueError, match="pixel limit"):
        image_loader.decode_thumbnail(data.getvalue())


@pytest.mark.parametrize(
    "url", ["file:///C:/private.png", "data:image/png;base64,AA", "ftp://example.com/a"]
)
def test_non_http_schemes_never_fetch(url):
    loader = image_loader.ImageLoader()
    failures = []
    loader.failed.connect(failures.append)
    loader.load(url)
    assert failures == [url]


@pytest.mark.parametrize("mode", ["declared", "stream", "timeout", "valid"])
def test_network_limits_and_success(monkeypatch, mode):
    monkeypatch.setattr(image_loader, "MAX_IMAGE_BYTES", 1024)
    monkeypatch.setattr(image_loader, "IMAGE_TIMEOUT_MS", 150)
    png = io.BytesIO()
    Image.new("RGB", (3, 2)).save(png, format="PNG")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            if mode == "declared":
                self.send_header("Content-Length", "1000000")
            self.end_headers()
            try:
                if mode == "timeout":
                    time.sleep(0.4)
                else:
                    self.wfile.write(png.getvalue() if mode == "valid" else b"x" * 2048)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    loader = image_loader.ImageLoader()
    failed, loaded = [], []
    loader.failed.connect(failed.append)
    loader.loaded.connect(loaded.append)
    url = f"http://127.0.0.1:{server.server_port}/{mode}"
    try:
        loader.load(url)
        deadline = time.monotonic() + 3
        while not failed and not loaded and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        assert bool(loaded) == (mode == "valid")
        assert failed == ([] if mode == "valid" else [url])
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
