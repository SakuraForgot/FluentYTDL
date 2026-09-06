"""频道标签页解析的 `unsupported` / `failed` 语义分离（P1-D）。

原来 `_extract_tab()` 的 `except Exception` 分支在 `logger.warning` 之后**照样**
`return tab, None, "unsupported"`，于是两件性质完全相反的事在日志和 UI 里长得一模一样：

- `unsupported` = 这个频道确实没有 Shorts 标签页 → 该永久隐藏，重取无意义
- `failed`      = 这一次没拿到（超时 / cookie 过期 / 风控）→ 必须让用户能重试

合并的代价不是少一行日志，是把一次网络抖动伪装成了频道的属性 —— 那个标签页在对话框
活着的期间再也不会被拉取一次。

这里锁死三件事：两个分支返回值不同、事件种类不同（`decision` vs `diagnosis`）、
`run()` 的收集循环真的把 `failed` 写回 results。

`run()` 里有 `QCoreApplication.translate` 和信号发射，需要 QApplication，走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-chtab-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from fluentytdl.download.workers import ChannelExtractWorker  # noqa: E402
from fluentytdl.youtube.youtube_service import YoutubeService  # noqa: E402


def _patch_service(monkeypatch, extract_channel_flat) -> None:
    """把 `extract_channel_flat` 换成桩 —— **必须打在类上，不能打在单例实例上**。

    `YoutubeService` 是 `__new__` 单例（`youtube_service.py:114`），所以
    `monkeypatch.setattr("...workers.youtube_service.extract_channel_flat", stub)`
    设的是**实例属性**；撤销时 monkeypatch 看到 `hasattr` 为真，就把原来的绑定方法
    照样 `setattr` 回**实例**上。于是这个实例从此永久带着一个实例属性 ——
    它会盖住别的测试文件在类上打的桩（`test_parse_cache.py` 正是这么打的），
    那三个用例会莫名其妙地读不到缓存。类级 setattr 撤销得干净。
    """
    monkeypatch.setattr(YoutubeService, "extract_channel_flat", extract_channel_flat)
    monkeypatch.setattr(YoutubeService, "build_ydl_options", lambda self, *a, **k: {})


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def events():
    captured: list[dict] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"]["fytdl"])),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _worker(tabs: list[str] | None = None) -> ChannelExtractWorker:
    return ChannelExtractWorker("https://example.invalid/@chan", tabs or ["shorts"])


def _raise(exc: Exception):
    def _stub(self, *a, **k):
        raise exc

    return _stub


# ── `_extract_tab` 的两条错误分支 ──────────────────────────────


def test_absent_tab_is_unsupported_not_failed(monkeypatch, events):
    """yt-dlp 的"该频道没有这个标签页"是事实，不是错误。

    因此只能是 `decision`：`count(kind=diagnosis)` 必须恒等于"真的出错了几次"
    （硬规则 2），把一个正常的频道形态记成 diagnosis 会让这个计数从此说谎。
    """
    worker = _worker()
    _patch_service(monkeypatch, _raise(Exception("This channel does not have a shorts tab")))

    tab, info, status = worker._extract_tab("shorts", {})

    assert (tab, info, status) == ("shorts", None, "unsupported")
    kinds = [e["kind"] for e in events]
    assert "diagnosis" not in kinds
    decision = next(e for e in events if e["kind"] == "decision")
    assert decision["status"] == "unsupported"
    assert decision["reason"] == "tab_absent"
    assert decision["stage"] == "parse"


def test_network_error_is_failed_and_diagnosed(monkeypatch, events):
    """断网 / 超时必须落 `failed` + 一条 diagnosis。

    这正是用户最初的痛点：以前这里只有一行 `logger.warning`，日志里读不出
    `code` / `category`，UI 上还被当成"这个频道没有 Shorts"。
    """
    worker = _worker()
    _patch_service(
        monkeypatch, _raise(Exception("Unable to download webpage: <urlopen error timed out>"))
    )

    tab, info, status = worker._extract_tab("shorts", {})

    assert (tab, info, status) == ("shorts", None, "failed")
    diag = next(e for e in events if e["kind"] == "diagnosis")
    assert diag["stage"] == "parse"
    assert diag["operation"] == "extract_channel_tab:shorts"
    assert diag["code"] and diag["category"]
    # 语言中立：日志里不许出现本地化文案（会随界面语言变，没法搜）
    assert "user_title" not in diag and "user_message" not in diag


def test_cancel_is_neither(monkeypatch, events):
    """用户取消不是错误，一条事件都不该有。"""
    from fluentytdl.youtube.yt_dlp_cli import YtDlpCancelled

    worker = _worker()
    _patch_service(monkeypatch, _raise(YtDlpCancelled()))

    assert worker._extract_tab("shorts", {})[2] == "cancelled"
    assert [e for e in events if e["kind"] in ("diagnosis", "decision")] == []


# ── `run()` 的收集循环 ────────────────────────────────────────


def test_run_writes_failed_into_results(qapp, monkeypatch):
    """`failed` 必须真的写回 results，否则 UI 拿到的还是 `unloaded`。

    三个标签页各走一条分支：一个成功、一个真没有、一个网络错。
    """

    def fake_extract(self, url, *args, tab="videos", **kwargs):
        if tab == "videos":
            return {"title": "Chan", "entries": [{"id": "a"}]}
        if tab == "shorts":
            raise Exception("This channel does not have a shorts tab")
        raise Exception("Unable to download webpage: <urlopen error timed out>")

    _patch_service(monkeypatch, fake_extract)

    worker = _worker(["videos", "shorts", "streams"])
    got: list[dict] = []
    worker.finished_all.connect(got.append)
    worker.run()  # 同步跑，不 start() —— 不需要真起线程

    assert len(got) == 1
    results = got[0]
    assert results["videos"]["status"] == "loaded"
    assert results["shorts"]["status"] == "unsupported"
    assert results["streams"]["status"] == "failed"
    assert results["streams"]["data"] is None


def test_run_does_not_report_batch_failure_when_one_tab_fails(qapp, monkeypatch):
    """单个标签页失败不许拖垮整批 —— `error` 信号不该响。

    整批的 `except` 会 emit 一条 `operation=extract_channel` 的 diagnosis；一个标签页
    的网络错误走的是子操作边界，两者不能混。
    """
    _patch_service(monkeypatch, _raise(Exception("boom")))

    worker = _worker(["videos"])
    errors: list[dict] = []
    finished: list[dict] = []
    worker.error.connect(errors.append)
    worker.finished_all.connect(finished.append)
    worker.run()

    assert errors == []
    assert finished == [{"videos": {"status": "failed", "data": None}}]
