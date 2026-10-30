from datetime import datetime

from vinyl import updates


def test_versions():
    assert updates.parse_version("v1.2.0") == (1, 2)
    assert updates.parse_version("1.10") > updates.parse_version("1.9.3")
    assert updates.is_newer("v1.2.1", "1.2.0")
    assert not updates.is_newer("v1.2.0", "1.2.0")
    assert not updates.is_newer("v1.2", "1.2.0")
    assert not updates.is_newer("v1.1.9", "1.2.0")


def test_asset_for_each_computer():
    assert updates.asset_name("win32", "AMD64") == "Vinyl-Recorder-windows.zip"
    assert updates.asset_name("darwin", "arm64") == "Vinyl-Recorder-mac-apple-silicon.zip"
    assert updates.asset_name("darwin", "x86_64") == "Vinyl-Recorder-mac-intel.zip"


def test_due_once_a_day():
    now = datetime(2026, 10, 30, 12)
    assert updates.due("", now)
    assert updates.due("garbage", now)
    assert not updates.due("2026-10-30T09:00:00", now)
    assert updates.due("2026-10-29T11:00:00", now)


def test_from_release():
    data = {"tag_name": "v9.0.0", "html_url": "https://example.com/r", "body": " New stuff ",
            "assets": [{"name": updates.asset_name(), "browser_download_url": "https://example.com/zip"}]}
    u = updates.from_release(data, "1.2.0")
    assert (u.version, u.page, u.download, u.notes) == ("9.0.0", "https://example.com/r", "https://example.com/zip",
                                                         "New stuff")
    assert updates.from_release({**data, "assets": []}, "1.2.0").download == "https://example.com/r"
    assert updates.from_release(data, "9.0.0") is None
    assert updates.from_release({**data, "prerelease": True}, "1.2.0") is None
    assert updates.from_release({}, "1.2.0") is None
