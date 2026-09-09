"""
Cookie 运行副本（run-file）隔离

yt-dlp 在**每次** `--cookies <file>` 运行结束时，都会把它内存里的 Cookie jar 回写进
那个文件（这是 yt-dlp 的固有行为，**没有任何 CLI 开关能关掉**）。一旦某个视频触发
YouTube 轮换/拒绝会话，yt-dlp 就会把「已登出」的 jar 回写覆盖真相源，永久抹掉
`.youtube.com` 上的 `LOGIN_INFO` / SID 家族——之后连正常视频都下不了。自伤且自我延续。

因此：**绝不把 Sentinel 托管的真相源路径直接交给 yt-dlp。** 每次 yt-dlp 调用前，把真相源
原样字节拷贝到一份用完即弃的临时文件，让 yt-dlp 的回写只污染这份副本；真相源
（`bin/cookies_youtube.txt` / `bin/cookies_twitter.txt`）在任何 yt-dlp 执行前后都保持
逐字节不变。

用户通过 `auth.cookies_file` 自行指定的外部 Cookie 文件属于用户自管，**不隔离**——
`_is_managed_truth_source()` 只对两个真相源返回 True。
"""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager

from ..utils.logger import logger

# 临时副本文件名前缀。`sweep_stale_cookie_runfiles()` 靠它做启动兜底清理。
_RUNFILE_PREFIX = "fluentytdl_ck_"
_RUNFILE_SUFFIX = ".txt"


def _is_managed_truth_source(path: str) -> bool:
    """`path` 是否是 Sentinel 托管的某个真相源（youtube / twitter）。

    用 `os.path.realpath` 归一化后比较，规避软链接 / 大小写 / 相对路径带来的漏判。
    托管真相源才隔离；用户自管文件与任何其它路径一律返回 False（走直通）。
    出现异常时保守地返回 False——绝不因为判断本身出错而误隔离用户文件。
    """
    try:
        from .cookie_sentinel import cookie_sentinel

        real = os.path.realpath(path)
        for platform in ("youtube", "twitter"):
            truth = str(cookie_sentinel.get_cookie_path_for_platform(platform))
            if real == os.path.realpath(truth):
                return True
    except Exception:
        return False
    return False


@contextmanager
def cookie_runfile(cookiefile: str | None) -> Iterator[str | None]:
    """产出一份 Sentinel 托管真相源的用完即弃字节副本，供 yt-dlp 的 `--cookies` 使用。

    yt-dlp 的运行结束回写只会落到这份副本上，真相源纹丝不动。非托管文件（用户自管）
    与 `None` 原样直通，不产生副本。

    临时文件用 `tempfile.mkstemp` 创建（系统级唯一命名、免竞态），拿到后**立刻**
    `os.close(fd)` 释放句柄，这样 yt-dlp 可以自由 reopen / truncate / rewrite。

    拷贝是**原始字节** `shutil.copyfile`——刻意**不做** `parse → CookieJar → normalize
    → dump`：那会在修掉「yt-dlp 二次污染」的同时凭空引入「FluentYTDL 二次清洗 Cookie」
    的新风险。

    副本在 `with` 块退出时（即 yt-dlp 子进程结束后）确定性删除。子进程被硬杀导致
    `finally` 没跑到时，由 `sweep_stale_cookie_runfiles()` 在下次启动兜底。
    """
    if not cookiefile or not _is_managed_truth_source(cookiefile):
        # 直通：用户自管文件 / 无 Cookie —— 不隔离、不产生副本。
        yield cookiefile
        return

    fd, tmp = tempfile.mkstemp(prefix=_RUNFILE_PREFIX, suffix=_RUNFILE_SUFFIX)
    os.close(fd)  # 交给 yt-dlp 之前必须先关掉句柄
    try:
        shutil.copyfile(cookiefile, tmp)  # 原始字节拷贝，绝不重新解析/清洗
        yield tmp
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def sweep_stale_cookie_runfiles(*, min_age_seconds: float = 3600.0) -> int:
    """启动兜底：清掉 `%TEMP%` 里遗留的 `fluentytdl_ck_*.txt` 运行副本。

    正常路径由 `cookie_runfile()` 的 `finally` 确定性删除；这里只回收「进程崩溃 /
    TerminateProcess / IDE 强杀 / 断电」导致 `finally` 没跑到的漏网文件，沿用
    `staging.gc_orphans` 的启动 GC 思路。

    `min_age_seconds` 保险：只删够旧的文件，避免误删另一个并发实例正在使用的副本
    （启动时通常无并发运行，但这层保险几乎零成本）。

    Returns: 实际删除的文件数。
    """
    pattern = os.path.join(tempfile.gettempdir(), f"{_RUNFILE_PREFIX}*{_RUNFILE_SUFFIX}")
    now = time.time()
    removed = 0
    for path in glob.glob(pattern):
        try:
            if now - os.path.getmtime(path) < min_age_seconds:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info(f"[cookie_runfile] 启动清理遗留运行副本: {removed} 个")
    return removed
