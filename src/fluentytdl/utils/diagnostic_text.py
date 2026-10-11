"""Small dependency-free redactor for component diagnostics."""

import re


def redact_detail(value):
    text = re.sub(r"(?i)(Generated POT:\s*)[^\s]+", r"\1[redacted]", str(value))
    text = re.sub(r"(?i)(https?|socks5h?)://[^\s/@]+@", r"\1://[redacted]@", text)
    text = re.sub(r"(?im)(authorization|cookie)(\s*:\s*)[^\r\n]+", r"\1\2[redacted]", text)
    text = re.sub(r"(?i)([?&](?:token|key|po_token|visitorData)=)[^&#\s]+", r"\1[redacted]", text)
    return re.sub(
        r"(?i)(token|cookie|authorization|visitorData)([\s\"':=]+)[^\s,}]+", r"\1\2[redacted]", text
    )[:1200]
