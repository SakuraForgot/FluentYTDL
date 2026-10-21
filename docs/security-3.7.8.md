# 3.7.8-rc.1 security remediation

Reviewed 2026-10-22. This release addresses the dependency and reachable-input findings
from the local source / published 3.7.7-rc.2 audit. It is not a claim that every future
vulnerability or every third-party binary is covered by a Python package scan.

| Boundary | Remediation | Evidence / remaining condition |
| --- | --- | --- |
| HTTPS trust (GHSA-6q48-6c7w-9gw2) | certifi >= 2026.10.19; remove affected roots when importing OS trust; same context for origin and HTTPS proxy; apply filtered roots to Qt image requests and builder downloads | Regression imports all four affected anchors into a simulated old OS store; none survive, unrelated roots and hostname verification remain enabled. |
| HTTP parsing / proxy TLS (CVE-2026-97687/97688/97689) | urllib3 >= 2.8.0 in application and independently packaged SABR core | Lock audit and SABR BUILD_INFO; hashed upstream requirements are replaced only when below security floors. |
| Qt SVG / network / JSON | Qt 6.12.0; no remote SVG decoding; image HTTP/2 disabled | Network thumbnails now enter only explicit Pillow raster decoders. QML, WebEngine and PDF remain excluded by the existing frozen build contract. |
| Pillow image parsing | Pillow >= 13.0.0; JPEG, PNG, WebP, GIF and BMP only | Actual decode tests, rejection of SVG/EPS, dimension check before conversion; 16 MiB / 16 million pixels / 30 second bounds and live local HTTP tests. |
| Python native libraries | Python 3.12.15, OpenSSL >= 3.5.9, Expat >= 2.8.5 | Actual runtime versions checked; packaging rejects older native libraries or mismatched locked packages. |
| POT redirects (CVE-2026-111755/111756) | follow-redirects 1.16.1 pinned by npm SHA512 in projected Deno production lock; local recipe revision 2 | Original upstream source/lock retained for provenance; only the security resolution changes. Real frozen Deno installation verified. Old retained POT trees cannot launch; component repair is required. |
| Developer tooling, ControlCenter (CVE-2026-93687) | Separate workspace migrated to Tailwind 4 and compatible tailwind-merge, eliminating braces dependency | npm audit: zero advisories; 14 Worker/protocol tests, typecheck, admin and Worker builds pass. This is a development dependency change; no production Worker deployment is needed to remove braces from developer installations. |

The 224 registry packages in the application lock returned no OSV findings at review
time. This does not scan embedded C libraries; the native-library checks above are
separate. Component acquisition always resolves current upstream assets and validates
their size/hashes; release publication never reuses this local diagnostic snapshot.

Existing app-core updates preserve external components and user-selected paths.
Users must update yt-dlp and repair POT in Settings / Components, or use the new full
portable / setup package. Custom executables remain user-owned. The stable channel
is unchanged by this prerelease; it must not be described as patched automatically.

Sources: [certifi advisory](https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2),
[Qt advisories](https://wiki.qt.io/List_of_known_vulnerabilities_in_Qt_products),
[Python 3.12.15](https://www.python.org/downloads/release/python-31215/),
[urllib3 advisories](https://github.com/urllib3/urllib3/security/advisories),
[Pillow advisories](https://github.com/python-pillow/Pillow/security/advisories),
[follow-redirects advisories](https://github.com/follow-redirects/follow-redirects/security/advisories).
