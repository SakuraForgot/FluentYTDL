from datetime import UTC, datetime, timedelta

import pytest

from fluentytdl.core.update_transport import UpdateError
from fluentytdl.notification.announcement_service import AnnouncementStore, eligible, validate


def item(**changes):
    return dict(id="a", revision=1, title="Title", body="## Text", language="all", **changes)


def test_ack_survives_restart_and_new_revision_requires_confirmation(tmp_path):
    store = AnnouncementStore(tmp_path / "announcements.sqlite3")
    notice = item(require_ack=True)
    store.mark(notice, "notified")
    store.mark(notice, "acknowledged")
    reopened = AnnouncementStore(store.path)
    assert reopened.state(notice) == (1, 1)
    assert reopened.state({**notice, "revision": 2}) == (0, 0)


def test_targeting_includes_prerelease_order_and_expiry():
    assert eligible(item(min_version="3.7.2-rc.1"), "3.7.2", "zh_CN")
    assert not eligible(item(min_version="3.7.2"), "3.7.2-rc.1", "zh_CN")
    assert not eligible({**item(), "language": "en_US"}, "3.7.2", "zh_CN")
    assert not eligible(item(expires_at="2020-01-01T00:00:00Z"), "3.7.2", "zh_CN")


def test_offline_cache_is_bounded_by_server_timestamp(tmp_path):
    store = AnnouncementStore(tmp_path / "announcements.sqlite3")
    data = dict(generated_at=datetime.now(UTC).isoformat(), announcements=[item()])
    store.save(data)
    assert store.cached() == data
    data["generated_at"] = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.save(data)
    with pytest.raises(UpdateError):
        store.cached()


def test_reject_duplicate_identity():
    with pytest.raises(UpdateError):
        validate(dict(generated_at=datetime.now(UTC).isoformat(), announcements=[item(), item()]))
