"""Keep compromised CA paths out of the updater's merged trust store."""

import hashlib
import re
import ssl
from pathlib import Path

import certifi
import requests
from PySide6.QtNetwork import QSsl, QSslCertificate

from fluentytdl.core import update_transport
from fluentytdl.utils import image_loader


def test_system_roots_cannot_restore_emudhra_trust(monkeypatch):
    pem = (Path(__file__).parent / "fixtures" / "emudhra-roots.pem.txt").read_text()
    blocked = [
        ssl.PEM_cert_to_DER_cert(value)
        for value in re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", pem, re.S
        )
    ]
    create_context = ssl.create_default_context
    trusted = create_context(cafile=certifi.where()).get_ca_certs(binary_form=True)
    assert len(blocked) == 4
    assert all(
        hashlib.sha256(root).hexdigest() in update_transport._DISTRUSTED_ROOT_SHA256
        for root in blocked
    )

    def system_context(*args, **kwargs):
        if kwargs.get("cafile"):
            return create_context(*args, **kwargs)
        # Model an older OS with all affected anchors, plus an unrelated root.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(
            cadata="\n".join(ssl.DER_cert_to_PEM_cert(r) for r in [*blocked, trusted[0]])
        )
        return context

    monkeypatch.setattr(ssl, "create_default_context", system_context)
    adapter = update_transport.SystemTLSAdapter()
    loaded = set(adapter.context.get_ca_certs(binary_form=True))
    assert not loaded.intersection(blocked)
    assert trusted[0] in loaded
    assert adapter.context.check_hostname
    assert adapter.context.verify_mode == ssl.CERT_REQUIRED
    assert adapter.poolmanager.connection_pool_kw["ssl_context"] is adapter.context
    proxy = adapter.proxy_manager_for("http://127.0.0.1:3128")
    assert proxy.connection_pool_kw["ssl_context"] is adapter.context
    assert proxy.proxy_config.ssl_context is adapter.context
    request = requests.Request("GET", "https://github.com/").prepare()
    _, pool = adapter.build_connection_pool_key_attributes(request, True)
    assert pool["ssl_context"] is adapter.context
    image_loader._thumbnail_tls.cache_clear()
    try:
        qt_roots = image_loader._thumbnail_tls().caCertificates()
        assert QSslCertificate(trusted[0], QSsl.EncodingFormat.Der) in qt_roots
        assert all(
            QSslCertificate(root, QSsl.EncodingFormat.Der) not in qt_roots for root in blocked
        )
    finally:
        image_loader._thumbnail_tls.cache_clear()
