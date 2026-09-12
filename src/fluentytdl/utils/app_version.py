"""Application version ordering, including historical installed versions."""

import re


def version_key(value: str) -> tuple[int, ...]:
    value = str(value).strip()
    legacy = re.fullmatch(r"(pre|beta)-(\d+\.\d+\.\d+)", value)
    if legacy:
        value = legacy[2] + ("-rc.0" if legacy[1] == "pre" else "-beta.0")
    value = re.sub(r"^v-?", "", value)
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-(rc|beta)\.(\d+))?", value)
    if not match:
        raise ValueError(f"Unsupported application version: {value}")
    return (
        *map(int, match.group(1, 2, 3)),
        {None: 2, "rc": 1, "beta": 0}[match[4]],
        int(match[5] or 0),
    )


def public_version(value: str, channel: str) -> bool:
    suffix = r"(?:-rc\.\d+)?" if channel == "pre" else ""
    return bool(re.fullmatch(r"v?\d+\.\d+\.\d+" + suffix, str(value)))
