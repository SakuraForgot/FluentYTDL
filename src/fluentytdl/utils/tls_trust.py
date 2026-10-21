"""Filtered OS and certifi trust shared by HTTPS clients and build tools."""

import hashlib
import ssl

import certifi

# Roots leading to the compromised eMudhra intermediates, including cross-signs.
# Source: https://emudhra.we-have-a-problem.com/roots.json (2026-10-21).
# OpenSSL's Windows root import does not apply the Windows Disallowed store.
# Filter these anchors before merging OS roots, even when certifi is current.
_DISTRUSTED_ROOT_SHA256 = frozenset(
    {
        "7dd78d5f4f13459a83dff9abbba62edbaf6f2d102bf257fd712f4d9f2746ed8d",
        "f2edaaff3d9b701fb4d14c9f285f8c0bad90fc3bcc1fa07d47f4718edaba34a5",
        "d7a7a0fb5d7e2731d771e9484ebcdef71d5f0c3e0a2948782bc83ee0ea699ef4",
        "86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b",
    }
)


def _tls_context():
    context = ssl.create_default_context(cafile=certifi.where())
    system_roots = ssl.create_default_context().get_ca_certs(binary_form=True)
    allowed = [
        ssl.DER_cert_to_PEM_cert(root)
        for root in system_roots
        if hashlib.sha256(root).hexdigest() not in _DISTRUSTED_ROOT_SHA256
    ]
    if allowed:
        context.load_verify_locations(cadata="\n".join(allowed))
    return context
