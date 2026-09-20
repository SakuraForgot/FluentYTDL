"""Isolated native tag reader. No GUI, application services or write APIs."""

from __future__ import annotations

import json
import os
import sys

from mutagen import id3
from mutagen.flac import FLAC
from mutagen.mp4 import MP4Tags
from mutagen.mp4._atom import Atoms
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

LIMIT = 8 * 1024 * 1024


class ReadBudgetExceeded(Exception):
    pass


class BudgetedFile:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = LIMIT

    def read(self, size=-1):
        # Ogg tail readers legitimately request read-to-EOF. Bound the actual
        # allocation, while allowing a short read smaller than the request.
        amount = self.remaining + 1 if size < 0 else min(size, self.remaining + 1)
        result = self.stream.read(amount)
        if len(result) > self.remaining:
            raise ReadBudgetExceeded()
        self.remaining -= len(result)
        return result

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def __getattr__(self, key):
        return getattr(self.stream, key)


def native_tags(path: str, kind: str) -> list[dict]:
    with open(path, "rb") as stream:
        source = BudgetedFile(stream)
        pictures = []
        if kind == "mp4":
            atoms = Atoms(source)
            try:
                atoms.path(b"moov", b"udta", b"meta", b"ilst")
            except KeyError:
                return []
            tags = MP4Tags(atoms, source)
        elif kind == "mp3":
            try:
                tags = id3.ID3(fileobj=source, translate=False)
            except id3.ID3NoHeaderError:
                return []
        else:
            media = {"flac": FLAC, "opus": OggOpus, "ogg": OggVorbis}[kind](fileobj=source)
            tags = media.tags or {}
            pictures = getattr(media, "pictures", [])
        rows = []
        for key, value in tags.items():
            if len(rows) >= 10000:
                raise ReadBudgetExceeded()
            binary = key in {"covr", "metadata_block_picture"} or hasattr(value, "data")
            if binary:
                values = getattr(value, "data", value)
                size = len(values) if isinstance(values, (bytes, str)) else sum(map(len, values))
                values = [{"binary_bytes_or_encoded_length": size, "omitted": True}]
            else:
                values = getattr(value, "text", value)
                if not isinstance(values, (list, tuple)):
                    values = [values]
                values = [
                    v.decode("utf-8", errors="replace")
                    if isinstance(v, bytes)
                    else v
                    if isinstance(v, (str, int, float, bool, list, tuple))
                    else str(v)
                    for v in values
                ]
            rows.append(
                {
                    "scope": "container",
                    "key": key,
                    "values": values,
                    "reader": "native",
                    "binary": binary,
                }
            )
        for index, picture in enumerate(pictures):
            rows.append(
                {
                    "scope": "container",
                    "key": f"PICTURE:{index}",
                    "reader": "native",
                    "binary": True,
                    "values": [{"mime": picture.mime, "bytes": len(picture.data), "omitted": True}],
                }
            )
        return rows


def pipe_stream(name: str, std_handle: int, mode: str):
    stream = getattr(sys, name, None)
    if stream is not None:
        return getattr(stream, "buffer", stream)
    # PyInstaller --windowed deliberately sets sys.std* to None. The parent
    # supplies inherited pipe handles, so open those handles explicitly.
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        get_handle = ctypes.windll.kernel32.GetStdHandle
        get_handle.restype = ctypes.c_void_p
        handle = get_handle(std_handle)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("missing_pipe")
        fd = msvcrt.open_osfhandle(handle, os.O_BINARY)
        return os.fdopen(fd, mode)
    raise OSError("missing_pipe")


def main() -> int:
    input_stream = pipe_stream("stdin", -10, "rb")
    output_stream = pipe_stream("stdout", -11, "wb")
    try:
        payload = input_stream.read(65537)
        if len(payload) > 65536:
            raise ReadBudgetExceeded()
        request = json.loads(payload)
        rows = native_tags(request["path"], request["kind"])
        result = {"tags": rows, "status": "ready"}
        data = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(data) > LIMIT:
            raise ReadBudgetExceeded()
    except ReadBudgetExceeded:
        data = b'{"status":"partial","error":"native_limit","tags":[]}'
    except Exception:
        data = b'{"status":"partial","error":"native_failed","tags":[]}'
    output_stream.write(data)
    output_stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
